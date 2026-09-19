"""Persistent skill plan/task state for Cowork workspace."""

from app.agent import run_state


def test_skill_workflow_creates_persistent_plan_and_task_transitions(client):
    project = client.post("/api/projects", json={"name": "Plan project"}).json()
    session = client.post(
        f"/api/projects/{project['id']}/sessions", json={"title": "Analyze API"},
    ).json()
    conn = client.app.state.conn
    run_id = run_state.create_run(
        conn, project["id"], session["id"], "msg_test", selected_skill="ba",
    )

    plan = run_state.create_skill_plan(
        conn, run_id, project["id"], session["id"], "ba",
        "Analyze the API specification", ["webapi-mock-server-spec.md"],
    )

    assert plan is not None
    assert plan["tasks"][0]["status"] == "completed"
    assert plan["tasks"][1]["status"] == "completed"
    assert plan["tasks"][2]["status"] == "running"

    run_state.activate_plan_step(conn, plan["id"], "generate_or_patch_artifact")
    written = run_state.complete_plan_step(
        conn, plan["id"], "generate_or_patch_artifact",
        artifact_ref="requirement.md",
    )
    assert written[0]["status"] == "completed"
    assert written[0]["artifact_refs"] == ["requirement.md"]

    response = client.get(
        f"/api/projects/{project['id']}/tasks?session_id={session['id']}"
    )
    assert response.status_code == 200
    persisted = response.json()["plans"][0]
    assert persisted["id"] == plan["id"]
    assert persisted["tasks"][0]["status"] == "completed"
    output_task = next(t for t in persisted["tasks"] if t["step_id"] == "generate_or_patch_artifact")
    assert output_task["artifact_refs"] == ["requirement.md"]


def test_task_status_supports_human_input_and_retry(client):
    project = client.post("/api/projects", json={"name": "Input project"}).json()
    session = client.post(
        f"/api/projects/{project['id']}/sessions", json={"title": "Clarify auth"},
    ).json()
    conn = client.app.state.conn
    run_id = run_state.create_run(conn, project["id"], session["id"], "msg_test")
    plan = run_state.create_skill_plan(
        conn, run_id, project["id"], session["id"], "ba",
        "Clarify auth", [],
    )
    assert plan is not None

    needs_input = run_state.update_plan_task(
        conn, plan["id"], "analyze_sources", "needs_input",
        error="Authentication is not specified.",
    )
    assert needs_input["status"] == "needs_input"
    retry = run_state.update_plan_task(conn, plan["id"], "analyze_sources", "running")
    assert retry["status"] == "running"
    failed = run_state.update_plan_task(
        conn, plan["id"], "analyze_sources", "failed", error="Source unavailable",
    )
    assert failed["status"] == "failed"


def test_cancelled_and_completed_plans_preserve_task_progress(client):
    project = client.post("/api/projects", json={"name": "Resume project"}).json()
    session = client.post(
        f"/api/projects/{project['id']}/sessions", json={"title": "Inspect spec"},
    ).json()
    conn = client.app.state.conn
    run_id = run_state.create_run(conn, project["id"], session["id"], "msg_test")
    plan = run_state.create_skill_plan(
        conn, run_id, project["id"], session["id"], "ba", "Inspect spec", [],
    )
    assert plan is not None

    run_state.complete_plan_step(conn, plan["id"], "analyze_sources")
    run_state.update_plan_task(conn, plan["id"], "identify_requirements", "running")
    run_state.update_plan_task(conn, plan["id"], "identify_gaps_and_conflicts", "skipped")
    run_state.finish_skill_plan(conn, plan["id"], succeeded=False, cancelled=True)
    cancelled = run_state.get_plan(conn, plan["id"])
    states = {task["step_id"]: task["status"] for task in cancelled["tasks"]}
    assert states["analyze_sources"] == "completed"
    assert states["identify_requirements"] == "pending"
    assert states["identify_gaps_and_conflicts"] == "skipped"

    run_state.update_plan_task(conn, plan["id"], "identify_requirements", "running")
    run_state.finish_skill_plan(conn, plan["id"], succeeded=True)
    completed = run_state.get_plan(conn, plan["id"])
    assert all(task["status"] in ("completed", "skipped") for task in completed["tasks"])


def test_waiting_plan_resumes_in_same_session_and_keeps_completed_tasks(client):
    project = client.post("/api/projects", json={"name": "Question project"}).json()
    session = client.post(
        f"/api/projects/{project['id']}/sessions", json={"title": "Clarify scope"},
    ).json()
    conn = client.app.state.conn
    first_run = run_state.create_run(conn, project["id"], session["id"], "msg_first")
    plan = run_state.create_skill_plan(
        conn, first_run, project["id"], session["id"], "ba", "Clarify scope", [],
    )
    assert plan is not None
    run_state.complete_plan_step(conn, plan["id"], "analyze_sources")
    run_state.update_plan_task(
        conn, plan["id"], "identify_requirements", "needs_input",
        error="Which API version should be included?",
    )
    run_state.update_run(conn, first_run, status="WAITING_USER")

    resumed_run = run_state.create_run(conn, project["id"], session["id"], "msg_answer")
    resumed = run_state.resume_waiting_plan(
        conn, project["id"], session["id"], resumed_run,
    )

    assert resumed is not None
    assert resumed["run_id"] == resumed_run
    statuses = {task["step_id"]: task["status"] for task in resumed["tasks"]}
    assert statuses["analyze_sources"] == "completed"
    assert statuses["identify_requirements"] == "running"
    response = client.get(f"/api/projects/{project['id']}/tasks?session_id={session['id']}")
    assert response.json()["plans"][0]["run_status"] == "RUNNING"
