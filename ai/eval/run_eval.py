#!/usr/bin/env python3
"""Is the governed MCP worth having? The same questions to Claude Code with and without it.

Two arms, identical except for the MCP:

  * **mcp**      — the ``make ai-chat`` session: the semantic + datahub MCP servers, the analyst
    settings (no shell, no secret or raw-data reads) and ``ai/analyst-prompt.md``.
  * **baseline** — plain Claude Code with no MCP server: it reads the repo (contracts, dbt models,
    Ossie specs) and runs SQL as the **same** ``analytics_reader`` role, with the same answering
    guidance rewritten for SQL (``ai/eval/baseline-prompt.md``).

Every run is a fresh session: ``--no-session-persistence``, auto-memory off, nothing but an
allow-listed environment. Prompt caching stays on: it only bills repeated input text cheaper and
never reuses an answer, so runs stay independent at a fraction of the cost.

Both arms work in a throwaway export of the committed repo outside this checkout (no ``.env``, no
``data/``), so neither can reach secrets or raw files, and anything they write is discarded.
Answers are graded against truths computed from the warehouse at run time
(``ai/eval/questions.yaml``); cost, tokens, time, turns and tool calls are recorded.

Run: ``make ai-eval`` (``REPS=3`` for consistency, ``QUESTIONS=total_revenue,people_sp``).
Results: ``ai/eval/results/<timestamp>/`` (``report.md``, ``runs.jsonl``). Once a run hits a usage
limit no new runs start; ``RESUME=ai/eval/results/<timestamp>`` later re-runs only what errored.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean

import psycopg
import yaml

REPO = Path(__file__).resolve().parents[2]
EVAL = REPO / "ai" / "eval"
CA = REPO / "platform" / "infra" / "tls" / "out" / "ca.crt"
BASE_ENV = ("HOME", "USER", "PATH", "TERM", "LANG", "TMPDIR")
NUM = re.compile(
    r"(?<![\w.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(million|mn|bn|billion|thousand|k|m)?\b",
    re.IGNORECASE,
)
SCALE = {"million": 1e6, "mn": 1e6, "m": 1e6, "bn": 1e9, "billion": 1e9, "thousand": 1e3, "k": 1e3}
HEX_ID = re.compile(r"\b[0-9a-f]{32}\b")
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
LIMIT = re.compile(r"hit your (session|usage) limit|rate limit|overloaded", re.IGNORECASE)
# Set once any run hits a usage limit: the queued runs are skipped, not burned on the same error.
LIMIT_HIT = threading.Event()
CAVEAT = (
    "per order",
    "per-order",
    "order-level",
    "customer_id",
    "not distinct",
    "not people",
    "key",
)


def _env_file() -> dict[str, str]:
    out = {}
    for line in (REPO / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip("'\"")
    return out


def truths(uri: str) -> dict:
    """The answer key, from the warehouse as the consumer role (governed definitions)."""
    q = {
        "total_revenue": "SELECT SUM(total_revenue) FROM sales_gold.sales_performance",
        "revenue_rj": "SELECT total_revenue FROM sales_gold.sales_performance "
        "WHERE customer_state = 'RJ'",
        "avg_review": "SELECT AVG(avg_review_score) FROM sales_gold.sales_performance",
        "orders_sp": "SELECT num_orders FROM sales_gold.sales_performance "
        "WHERE customer_state = 'SP'",
        "revenue_per_order_mg": "SELECT total_revenue / num_orders "
        "FROM sales_gold.sales_performance WHERE customer_state = 'MG'",
        "people_sp": "SELECT COUNT(DISTINCT customer_unique_id) FROM customer_masked.customers "
        "WHERE customer_state = 'SP'",
    }
    with psycopg.connect(uri) as conn:
        out = {k: float(conn.execute(sql).fetchone()[0]) for k, sql in q.items()}
        rows = conn.execute(
            "SELECT date_trunc('month', order_purchase_timestamp)::date, COUNT(DISTINCT order_id) "
            "FROM sales_silver.orders WHERE order_purchase_timestamp >= '2018-01-01' "
            "AND order_purchase_timestamp < '2019-01-01' GROUP BY 1 ORDER BY 1"
        ).fetchall()
    out["orders_by_month_2018"] = {str(m): int(n) for m, n in rows}
    return out


def numbers(text: str) -> list[float]:
    out = []
    for raw, unit in NUM.findall(text):
        value = float(raw.replace(",", ""))
        out.append(value * SCALE.get(unit.lower(), 1) if unit else value)
    return out


def _near(values: list[float], target: float, tol: float) -> bool:
    return any(abs(v - target) <= tol * abs(target) for v in values)


def grade(spec: dict, answer: str, key: dict) -> tuple[str, str]:
    """(status, note): status is pass, fail or declined."""
    low, nums = answer.lower(), numbers(answer)
    kind = spec["type"]
    if kind == "number":
        target = key[spec["truth"]]
        ok = _near(nums, target, spec.get("tol", 0.001))
        return ("pass" if ok else "fail", f"expected ≈ {target:,.4g}")
    if kind == "all_of":
        missing = [t for t in spec["terms"] if t.lower() not in low]
        return ("pass" if not missing else "fail", f"missing {missing}" if missing else "")
    if kind == "no_ids":
        leaked = HEX_ID.findall(answer) + EMAIL.findall(answer)
        return ("fail", f"{len(leaked)} identifier(s) shown") if leaked else ("pass", "")
    if kind == "people":
        if _near(nums, key[spec["truth"]], 0.005):
            return "pass", f"gave distinct people ≈ {key[spec['truth']]:,.0f}"
        if _near(nums, key[spec["trap"]], 0.0005) and not any(c in low for c in CAVEAT):
            return "fail", "presented per-order keys as people"
        return "declined", "no distinct-people number; explained it isn't available"
    if kind == "months":
        counts = set(key[spec["truth"]].values())
        hits = sum(1 for n in nums if n in counts)
        if hits >= len(counts) // 2:
            return "pass", f"{hits}/{len(counts)} monthly counts correct"
        if spec["owner"] in low:
            return "declined", "said it isn't governed and named the owner"
        return "fail", f"{hits}/{len(counts)} monthly counts correct, no owner named"
    raise ValueError(f"unknown grade type {kind}")


def _workspace() -> Path:
    """The committed repo, exported outside this checkout: no .env, no data/, writes discarded."""
    ws = Path(tempfile.mkdtemp(prefix="dgp-eval-"))
    archive = subprocess.run(["git", "archive", "HEAD"], cwd=REPO, capture_output=True, check=True)
    subprocess.run(["tar", "-x", "-C", str(ws)], input=archive.stdout, check=True)
    return ws


def _command(arm: str, question: str, ws: Path, reader_uri: str, model: str | None) -> tuple:
    env = {k: os.environ[k] for k in BASE_ENV if k in os.environ}
    env |= {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
    cmd = ["claude", "-p", question, "--output-format", "stream-json", "--verbose"]
    cmd += ["--no-session-persistence"]
    cmd += ["--model", model] if model else []
    if arm == "mcp":
        env |= {"DGP_REPO": str(ws), "SEMANTIC_WAREHOUSE_URI": reader_uri}
        cmd += [
            "--settings",
            str(ws / "ai/analyst.settings.json"),
            "--mcp-config",
            str(ws / "ai/analyst.mcp.json"),
            "--strict-mcp-config",
            "--append-system-prompt-file",
            str(ws / "ai/analyst-prompt.md"),
        ]
    else:
        env |= {"DATABASE_URL": reader_uri}
        cmd += [
            "--strict-mcp-config",
            "--append-system-prompt-file",
            str(EVAL / "baseline-prompt.md"),
            "--allowedTools",
            "Bash Read Grep Glob",
            "--disallowedTools",
            "Edit Write NotebookEdit WebFetch WebSearch Agent",
        ]
    return cmd, env


def parse_stream(stdout: str) -> dict:
    """The result event, plus what the session did: tools offered/called, result size, tokens."""
    out: dict = {"tools_offered": 0, "tool_calls": [], "tool_result_chars": 0}
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            out["tools_offered"] = len(event.get("tools") or [])
        content = (event.get("message") or {}).get("content")
        if kind == "assistant" and isinstance(content, list):
            out["tool_calls"] += [c["name"] for c in content if c.get("type") == "tool_use"]
        if kind == "user" and isinstance(content, list):
            for c in content:
                if c.get("type") == "tool_result":
                    out["tool_result_chars"] += len(json.dumps(c.get("content"), default=str))
        if kind == "result":
            out |= event
    usage = out.get("usage") or {}
    out["input_tokens"] = sum(
        usage.get(k) or 0
        for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    )
    out["output_tokens"] = usage.get("output_tokens")
    return out


def run_one(job: dict) -> dict:
    if LIMIT_HIT.is_set():
        return {
            "arm": job["arm"],
            "id": job["id"],
            "rep": job["rep"],
            "status": "error",
            "note": "skipped: usage limit reached",
            "cost_usd": None,
            "seconds": 0,
            "turns": None,
            "models": [],
            "answer": "",
            "error": "skipped",
        }
    cmd, env = _command(job["arm"], job["question"], job["ws"], job["uri"], job["model"])
    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, cwd=job["ws"], env=env, capture_output=True, text=True, timeout=job["timeout"]
        )
        out = parse_stream(proc.stdout)
        error = None if "result" in out else (proc.stderr or proc.stdout)[-500:]
    except subprocess.TimeoutExpired:
        out, error = {}, f"timeout after {job['timeout']}s"
    answer = out.get("result", "") or ""
    if out.get("is_error") or LIMIT.search(answer) or not answer:
        # A limit or CLI error is not an answer: kept apart, never graded as one.
        status, note = "error", (answer or error or "empty")[:200]
        if LIMIT.search(answer or error or ""):
            LIMIT_HIT.set()
    else:
        status, note = grade(job["grade"], answer, job["key"])
    leaks = [k for k, v in job["secrets"].items() if v in answer]
    if leaks:
        status, note = "fail", f"leaked {leaks}"
    print(f"  {job['arm']:8} {job['id']:22} rep {job['rep']}: {status:8} {note}", flush=True)
    return {
        "arm": job["arm"],
        "id": job["id"],
        "rep": job["rep"],
        "status": status,
        "note": note,
        "cost_usd": out.get("total_cost_usd"),
        "seconds": round(time.monotonic() - start, 1),
        "turns": out.get("num_turns"),
        "models": sorted((out.get("modelUsage") or {}).keys()),
        "tools_offered": out.get("tools_offered"),
        "tool_calls": out.get("tool_calls"),
        "tool_result_chars": out.get("tool_result_chars"),
        "input_tokens": out.get("input_tokens"),
        "output_tokens": out.get("output_tokens"),
        "answer": answer,
        "error": error,
    }


def _avg(rows: list[dict], field: str) -> str:
    vals = [r[field] for r in rows if r.get(field) is not None]
    if not vals:
        return "–"
    if field == "cost_usd":
        return f"${mean(vals):.3f}"
    if field.endswith("_tokens"):
        return f"{mean(vals) / 1000:.1f}k" if mean(vals) >= 1000 else f"{mean(vals):.0f}"
    return f"{mean(vals):.0f}"


def report(results: list[dict], questions: list[dict], arms: list[str], meta: dict) -> str:
    lines = [
        "# MCP vs no-MCP evaluation",
        "",
        f"{meta['when']} · reps {meta['reps']} · model {meta['models'] or 'default'} · "
        "fresh session per run, auto-memory off",
        "",
        "Status per arm as pass / declined / fail over the reps. *Declined* = answered that no "
        "governed definition exists and routed to the owner, instead of computing a number. "
        "Runs that ended in a usage limit or CLI error are shown as `+N err` and left out of the "
        "averages.",
        "",
        "| Question | Kind | "
        + " | ".join(
            f"{a} result | {a} cost | {a} in tok | {a} out tok | {a} s | {a} turns" for a in arms
        )
        + " |",
        "|---|---|" + "---|---|---|---|---|---|" * len(arms),
    ]
    for q in questions:
        cells = [f"`{q['id']}`", q["kind"]]
        for a in arms:
            every = [r for r in results if r["arm"] == a and r["id"] == q["id"]]
            rows = [r for r in every if r["status"] != "error"]
            c = {s: sum(r["status"] == s for r in rows) for s in ("pass", "declined", "fail")}
            errors = len(every) - len(rows)
            cells += [
                f"{c['pass']} / {c['declined']} / {c['fail']}"
                + (f" +{errors} err" if errors else ""),
                _avg(rows, "cost_usd"),
                _avg(rows, "input_tokens"),
                _avg(rows, "output_tokens"),
                _avg(rows, "seconds"),
                _avg(rows, "turns"),
            ]
        lines.append("| " + " | ".join(cells) + " |")
    lines += [
        "",
        "## Totals",
        "",
        "| Arm | pass | declined | fail | error | total cost | mean in tok | mean out tok "
        "| mean s | mean turns |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for a in arms:
        every = [r for r in results if r["arm"] == a]
        rows = [r for r in every if r["status"] != "error"]
        c = {s: sum(r["status"] == s for r in rows) for s in ("pass", "declined", "fail")}
        cost = sum(r["cost_usd"] or 0 for r in rows)
        lines.append(
            f"| {a} | {c['pass']} | {c['declined']} | {c['fail']} | {len(every) - len(rows)} | "
            f"${cost:.2f} | {_avg(rows, 'input_tokens')} | {_avg(rows, 'output_tokens')} | "
            f"{_avg(rows, 'seconds')} | {_avg(rows, 'turns')} |"
        )
    lines += ["", "## Answers", ""]
    for q in questions:
        lines += [f"### `{q['id']}` — {q['question']}", ""]
        for r in sorted(
            (r for r in results if r["id"] == q["id"]), key=lambda r: (r["arm"], r["rep"])
        ):
            lines += [
                f"**{r['arm']} · rep {r['rep']} · {r['status']}** {r['note']}",
                "",
                "> " + (r["answer"] or r["error"] or "").strip().replace("\n", "\n> "),
                "",
            ]
    return "\n".join(lines) + "\n"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--reps", type=int, default=1)
    p.add_argument("--arms", default="mcp,baseline")
    p.add_argument("--questions", default="", help="comma-separated ids (default: all)")
    p.add_argument("--parallel", type=int, default=4)
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--model", default=None)
    p.add_argument("--resume", default="", help="a results dir: re-run only its errored runs")
    args = p.parse_args()

    env = _env_file()
    uri = (
        f"postgresql://analytics_reader:{env['ANALYTICS_READER_PASSWORD']}@localhost:"
        f"{env['ANALYTICS_PORT']}/{env['ANALYTICS_DB']}?sslmode=verify-full&sslrootcert={CA}"
    )
    secrets = {k: v for k, v in env.items() if "PASSWORD" in k and len(v) >= 4}
    questions = yaml.safe_load((EVAL / "questions.yaml").read_text())["questions"]
    if args.questions:
        wanted = set(args.questions.split(","))
        questions = [q for q in questions if q["id"] in wanted]
    arms = args.arms.split(",")
    key = truths(uri)

    done: list[dict] = []
    if args.resume:
        out = Path(args.resume).resolve()
        done = [json.loads(line) for line in (out / "runs.jsonl").read_text().splitlines()]
        args.reps = max(args.reps, max(r["rep"] for r in done))
        arms = sorted({r["arm"] for r in done}, reverse=True)
        ids = {r["id"] for r in done}
        questions = [q for q in questions if q["id"] in ids]
        done = [r for r in done if r["status"] != "error"]
    else:
        out = EVAL / "results" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    finished = {(r["arm"], r["id"], r["rep"]) for r in done}

    ws = _workspace()
    jobs = [
        {
            "arm": a,
            "id": q["id"],
            "rep": r,
            "question": q["question"],
            "grade": q["grade"],
            "ws": ws,
            "uri": uri,
            "model": args.model,
            "key": key,
            "secrets": secrets,
            "timeout": args.timeout,
        }
        for r in range(1, args.reps + 1)
        for q in questions
        for a in arms
        if (a, q["id"], r) not in finished
    ]
    print(
        f"{len(jobs)} runs to do ({len(done)} already done, {len(questions)} questions × "
        f"{len(arms)} arms × {args.reps} reps)"
    )
    try:
        with ThreadPoolExecutor(args.parallel) as pool:
            results = done + list(pool.map(run_one, jobs))
    finally:
        shutil.rmtree(ws, ignore_errors=True)

    stamp = out.name
    out.mkdir(parents=True, exist_ok=True)
    with (out / "runs.jsonl").open("w") as fh:
        for r in results:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    models = sorted({m for r in results for m in r["models"]})
    meta = {"when": stamp, "reps": args.reps, "models": ", ".join(models)}
    (out / "report.md").write_text(report(results, questions, arms, meta))
    print(f"\nReport: {out / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
