"use client";

import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { Check, Circle, CircleAlert, LoaderCircle, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api, TaskStatus, WorkPlan } from "@/lib/api";

const STATUS_LABEL: Record<TaskStatus, string> = {
  pending: "To do", running: "In progress", completed: "Completed",
  needs_input: "Needs your input", failed: "Failed", skipped: "Skipped",
};

function planStatusLabel(status: string) {
  const labels: Record<string, string> = {
    RUNNING: "In progress", SUCCEEDED: "Completed", FAILED: "Failed",
    CANCELLED: "Stopped", WAITING_USER: "Needs your input",
  };
  return labels[status] || status.toLowerCase();
}

function StatusIcon({ status }: { status: TaskStatus }) {
  if (status === "completed") return <Check className="task-icon completed" />;
  if (status === "running") return <LoaderCircle className="task-icon running" />;
  if (status === "needs_input") return <CircleAlert className="task-icon needs-input" />;
  if (status === "failed") return <X className="task-icon failed" />;
  return <Circle className="task-icon pending" />;
}

export default function TasksPage() {
  const params = useParams();
  const pid = String(params.projectId);
  const [plans, setPlans] = useState<WorkPlan[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    const refresh = () => api.workPlans(pid).then((r) => setPlans(r.plans)).catch((e) => setError(String(e)));
    refresh();
    const timer = window.setInterval(refresh, 2000);
    return () => window.clearInterval(timer);
  }, [pid]);

  return (
    <div className="chatlog tasks-page">
      <header className="tasks-heading">
        <div><div className="eyebrow">PROJECT WORK</div><h1>Tasks</h1><p>Plans and task progress from skill-assisted work.</p></div>
        <Badge variant="outline">{plans.length} {plans.length === 1 ? "plan" : "plans"}</Badge>
      </header>
      {error && <div className="error-text">{error}</div>}
      {plans.length === 0 && !error && (
        <Card className="tasks-empty"><CardContent><h2>No plans yet</h2><p>When you start a task with an enabled skill, its plan and progress will appear here.</p><a href={`/projects/${pid}`}>Start a conversation →</a></CardContent></Card>
      )}
      <div className="plans-list">
        {plans.map((plan) => {
          const done = plan.tasks.filter((task) => task.status === "completed").length;
          const current = plan.tasks.find((task) => task.status === "running" || task.status === "needs_input" || task.status === "failed");
          return (
            <Card key={plan.id} className="plan-card">
              <CardHeader>
                <div className="plan-heading-row"><div><div className="eyebrow">{plan.skill_id}</div><CardTitle>{plan.goal}</CardTitle><p>{plan.summary}</p></div><Badge variant={plan.run_status === "RUNNING" ? "secondary" : "outline"}>{planStatusLabel(plan.run_status)}</Badge></div>
                <div className="plan-progress-label"><span>{current?.title || (done === plan.tasks.length ? "All tasks complete" : "Plan progress")}</span><span>{done} / {plan.tasks.length}</span></div>
                <div className="plan-progress"><span style={{ width: `${plan.tasks.length ? done / plan.tasks.length * 100 : 0}%` }} /></div>
              </CardHeader>
              <CardContent>
                <ol className="task-list">
                  {plan.tasks.map((task) => (
                    <li key={task.id} className={`task-row ${task.status}`}>
                      <StatusIcon status={task.status} />
                      <div className="task-copy"><strong>{task.title}</strong>{(task.status === "needs_input" || task.status === "failed") && task.error && <p>{task.error}</p>}{task.source_refs.length > 0 && <small>{task.source_refs.join(", ")}</small>}</div>
                      <span className="task-status">{STATUS_LABEL[task.status]}</span>
                    </li>
                  ))}
                </ol>
                {plan.sources.length > 0 && <div className="plan-sources"><span>Sources</span>{plan.sources.map((source) => <Badge key={source} variant="outline">{source}</Badge>)}</div>}
              </CardContent>
            </Card>
          );
        })}
      </div>
    </div>
  );
}
