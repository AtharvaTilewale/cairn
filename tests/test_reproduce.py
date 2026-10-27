"""Tests for the reproduce agent with a scripted model (no network)."""

import json
import sys
from types import SimpleNamespace

import pytest

from cairnlab.agent.reproduce import MAX_PLAN_CALLS, ReproduceAgent, verdict_from
from cairnlab.provenance.graph import resolve_version
from tests.test_gather import ROOT, Store, T

STABLE = b"""import sys
total = sum(float(x) for x in open(sys.argv[1]).read().split())
open(sys.argv[2], "w").write(f"total {total}\\n")
"""
NOISY = b"""import sys, time
open(sys.argv[2], "w").write(f"total {time.time()}\\n")
"""


def make_store(tmp_path, script: bytes = STABLE, output: bytes = b"total 6.5\n"):
    s = Store(tmp_path)
    s.file("sum.py", T("08:00:00"), script, op="scanned")
    s.file("in.txt", T("08:00:00"), b"1\n2\n3.5\n", op="scanned")
    s.cmd(f"{sys.executable} sum.py in.txt total.txt", T("09:00:00"), T("09:00:02"))
    s.file("total.txt", T("09:00:01"), output, op="created")
    s.cmd("sleep 3000; cp total.txt slow.txt", T("10:00:00"), T("10:50:00"))
    s.file("slow.txt", T("10:49:59"), output, op="created")
    s.load()
    return s


def call(name: str, **args):
    return SimpleNamespace(
        id=f"c-{name}-{len(json.dumps(args))}",
        function=SimpleNamespace(name=name, arguments=json.dumps(args)),
    )


def msg(*calls, content=""):
    return SimpleNamespace(content=content, tool_calls=list(calls))


class Scripted:
    """A fake model: each turn is a function of the conversation so far."""

    model = "muse-spark-1.3"

    def __init__(self, turns):
        self.turns = list(turns)
        self.seen = []

    def complete(self, *, purpose, messages, tools, reasoning_effort):
        self.seen.append(
            {"tools": [t["function"]["name"] for t in tools], "effort": reasoning_effort}
        )
        turn = self.turns.pop(0) if self.turns else (lambda m: msg(call("get_trace")))
        return turn(messages)


def last_result(messages) -> dict:
    return json.loads(next(m for m in reversed(messages) if m["role"] == "tool")["content"])


def happy_turns():
    state = {}

    def plan(messages):
        trace = last_result(messages)
        state["final"] = trace["steps"][-1]["run_id"]
        state["target"] = trace["target"]["version_id"]
        return msg(
            call(
                "propose_plan",
                summary="Re-run the final step.",
                steps=[{"run_id": state["final"], "check": [state["target"]], "why": "cheap"}],
            )
        )

    return [
        lambda m: msg(call("get_trace")),
        plan,
        lambda m: msg(call("run_in_sandbox", run_id=state["final"])),
        lambda m: msg(call("compare", run_id=state["final"], version_id=state["target"])),
        lambda m: msg(call("finish", explanation="total.txt was regenerated identically.")),
    ]


def agent_for(s, path, client, approve=True, tmp_path=None):
    vid = resolve_version(s.conn, "hp", f"{ROOT}/{path}")
    plans = []

    def confirm(plan):
        plans.append(plan)
        return approve

    a = ReproduceAgent(client, s.conn, s.objects, vid, confirm, scratch_parent=tmp_path)
    return a, plans


def test_happy_path_reproduces_identically(tmp_path):
    s = make_store(tmp_path)
    client = Scripted(happy_turns())
    agent, plans = agent_for(s, "total.txt", client, tmp_path=tmp_path)
    report = agent.run()
    assert report.verdict == "reproduced"
    assert [c.outcome for c in report.checks] == ["identical"]
    assert len(plans) == 1 and plans[0].steps[0].cmd.endswith("sum.py in.txt total.txt")
    assert report.explanation.startswith("total.txt was regenerated")
    # Read-only tools before approval, execution tools after; high reasoning effort.
    assert "run_in_sandbox" not in client.seen[0]["tools"]
    assert "run_in_sandbox" in client.seen[2]["tools"]
    assert {t["effort"] for t in client.seen} == {"high"}
    assert report.tool_calls == 5


def test_declined_plan_runs_nothing(tmp_path):
    s = make_store(tmp_path)
    turns = happy_turns()[:2] + [lambda m: msg(call("finish", explanation="declined"))]
    agent, _ = agent_for(s, "total.txt", Scripted(turns), approve=False, tmp_path=tmp_path)
    report = agent.run()
    assert report.verdict == "inconclusive" and report.replays == []


def test_nothing_runs_during_planning(tmp_path):
    s = make_store(tmp_path)
    seen = []

    def check_refusal(messages):
        seen.append(last_result(messages))
        return msg(call("get_trace"))

    turns = happy_turns()
    turns = [lambda m: msg(call("run_in_sandbox", run_id="anything")), check_refusal, *turns[1:]]
    agent, _ = agent_for(s, "total.txt", Scripted(turns), tmp_path=tmp_path)
    report = agent.run()
    assert seen[0] == {"error": "run_in_sandbox is not available now"}
    assert report.verdict == "reproduced" and len(report.replays) == 1


def test_unplanned_run_is_refused(tmp_path):
    s = make_store(tmp_path)
    state = {}

    def plan(messages):
        tr = last_result(messages)
        state["final"] = tr["steps"][0]["run_id"]  # total.txt's step
        return msg(
            call(
                "propose_plan",
                summary="x",
                steps=[{"run_id": state["final"], "check": [], "why": "w"}],
            )
        )

    def sneaky(messages):
        return msg(call("run_in_sandbox", run_id="01ARZ3NDEKTSV4RRFFQ69G5FAV"))

    results = []

    def record_then_finish(messages):
        results.append(last_result(messages))
        return msg(call("finish", explanation="done"))

    agent, _ = agent_for(
        s,
        "total.txt",
        Scripted([lambda m: msg(call("get_trace")), plan, sneaky, record_then_finish]),
        tmp_path=tmp_path,
    )
    report = agent.run()
    assert results[0] == {"error": "only approved plan steps can be run"}
    assert report.replays == []


def test_long_step_is_refused_by_policy(tmp_path):
    s = make_store(tmp_path)

    def plan(messages):
        tr = last_result(messages)
        slow = next(st for st in tr["steps"] if st["cmd"].startswith("sleep"))
        assert slow["replayable"] is False and "50.0 min" in slow["why_not"]
        return msg(
            call(
                "propose_plan",
                summary="try anyway",
                steps=[{"run_id": slow["run_id"], "check": [], "why": "w"}],
            )
        )

    agent, plans = agent_for(
        s,
        "slow.txt",
        Scripted(
            [
                lambda m: msg(call("get_trace")),
                plan,
                lambda m: msg(call("finish", explanation="The final step took 50 min.")),
            ]
        ),
        tmp_path=tmp_path,
    )
    report = agent.run()
    assert report.verdict == "refused" and plans == []  # nothing to approve
    assert "50.0 min" in report.plan.rejected[0]


def test_nondeterministic_output_is_not_reproduced(tmp_path):
    s = make_store(tmp_path, script=NOISY, output=b"total 1761556443.1\n")
    agent, _ = agent_for(s, "total.txt", Scripted(happy_turns()), tmp_path=tmp_path)
    report = agent.run()
    assert report.verdict == "not_reproduced"
    assert report.checks[0].outcome == "different"


def test_tool_call_budget(tmp_path):
    s = make_store(tmp_path)
    client = Scripted([lambda m: msg(call("get_trace"))] * 40)
    agent, _ = agent_for(s, "total.txt", client, tmp_path=tmp_path)
    report = agent.run()
    assert report.tool_calls == MAX_PLAN_CALLS and report.verdict == "inconclusive"
    # Near the end of the planning budget only propose_plan is offered.
    assert client.seen[-1]["tools"] == ["propose_plan"]


def test_verdicts():
    from cairnlab.agent.reproduce import CheckResult, Plan, PlanStep

    plan = Plan("s", [PlanStep("r", ["v"], "w")])
    ok = CheckResult("r", "v", "p", "identical", "")
    close = CheckResult("r", "v", "p", "close", "")
    bad = CheckResult("r", "v", "p", "different", "")
    assert verdict_from([ok], plan, True) == "reproduced"
    assert verdict_from([ok, close], plan, True) == "reproduced_close"
    assert verdict_from([ok, bad], plan, True) == "not_reproduced"
    assert verdict_from([], plan, True) == "inconclusive"
    assert verdict_from([], Plan("s", [], ["too long"]), False) == "refused"


@pytest.mark.parametrize("name", ["read_file", "estimate_cost", "list_files"])
def test_read_only_tools(tmp_path, name):
    s = make_store(tmp_path)
    vid = resolve_version(s.conn, "hp", f"{ROOT}/total.txt")
    agent = ReproduceAgent(None, s.conn, s.objects, vid, lambda p: True)
    final = agent.trace.steps[-1]
    if name == "read_file":
        script = next(v for v in final.inputs if agent.trace.nodes[v].path.endswith("sum.py"))
        assert "sum(float" in agent.tool_read_file(script)["text"]
    elif name == "estimate_cost":
        got = agent.tool_estimate_cost(final.run["id"])
        assert got["original_duration_s"] == 2.0 and got["replayable"] is True
    else:
        files = agent.tool_list_files(final.run["id"])["files"]
        assert {f["path"].rsplit("/", 1)[-1] for f in files} == {"sum.py", "in.txt"}


def test_extra_inputs_must_be_files_that_existed(tmp_path):
    s = make_store(tmp_path)
    vid = resolve_version(s.conn, "hp", f"{ROOT}/total.txt")
    agent = ReproduceAgent(None, s.conn, s.objects, vid, lambda p: True)
    final = agent.trace.steps[-1].run["id"]
    later = resolve_version(s.conn, "hp", f"{ROOT}/slow.txt")  # written after the run
    res = agent.tool_propose_plan(
        "x", [{"run_id": final, "check": [], "why": "w", "extra_inputs": [later]}]
    )
    assert res["approved"] is False and "list_files" in agent.plan.rejected[0]


def test_propose_plan_tolerates_malformed_arguments(tmp_path):
    s = make_store(tmp_path)
    vid = resolve_version(s.conn, "hp", f"{ROOT}/total.txt")
    agent = ReproduceAgent(None, s.conn, s.objects, vid, lambda p: True)
    final = agent.trace.steps[-1].run["id"]
    res = agent.tool_propose_plan(steps=json.dumps([{"run_id": final, "check": [], "why": "w"}]))
    assert res["approved"] is True and agent.plan.steps[0].run_id == final
    assert agent.tool_propose_plan()["approved"] is False
