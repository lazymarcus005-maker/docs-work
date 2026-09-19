"use client";

import { useParams } from "next/navigation";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, ProjectFile, RunEvent, Skill, SkillTool } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

const emptyDraft = {
  id: "",
  name: "",
  description: "",
  prompt: "",
  tools: [] as string[],
  enabled_for_project: true,
};

export default function SkillsPage() {
  const params = useParams();
  const pid = String(params.projectId);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [files, setFiles] = useState<ProjectFile[]>([]);
  const [toolCatalog, setToolCatalog] = useState<SkillTool[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [instruction, setInstruction] = useState("");
  const [runningSkill, setRunningSkill] = useState<string | null>(null);
  const [progress, setProgress] = useState("");
  const [result, setResult] = useState("");
  const [error, setError] = useState("");
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [draft, setDraft] = useState(emptyDraft);
  const [savingSkill, setSavingSkill] = useState(false);
  const [skillIdEdited, setSkillIdEdited] = useState(false);

  const refresh = useCallback(() => {
    api.projectSkills(pid).then((r) => setSkills(r.skills)).catch((e) => setError(String(e)));
    api.listFiles(pid).then((r) => setFiles(r.files)).catch(() => {});
  }, [pid]);

  useEffect(() => {
    refresh();
    api.skillToolCatalog().then((r) => setToolCatalog(r.tools)).catch(() => {});
  }, [refresh]);

  function handleEvent(e: RunEvent) {
    if (e.event === "skill.started") {
      setRunningSkill(e.data.skill);
      setProgress(`Running ${e.data.skill}…`);
    } else if (e.event === "skill.completed") {
      setProgress(`${e.data.skill} completed`);
      setResult(`Output: ${(e.data.artifacts || []).join(", ") || "(no files)"}`);
    } else if (e.event === "tool.started") {
      setProgress(`Tool: ${e.data.tool}…`);
    } else if (e.event === "run.completed") {
      setRunningSkill(null);
      setProgress("");
      if (!result) setResult(e.data.status === "SUCCEEDED" ? "Done." : `Run ${e.data.status}`);
    } else if (e.event === "run.failed") {
      setRunningSkill(null);
      setProgress("");
      setError(`${e.data.message}\n${(e.data.actions || []).join("\n")}`);
    }
  }

  async function run(skill: Skill) {
    setError("");
    setResult("");
    try {
      await api.runSkill(pid, skill.id, {
        instruction: instruction || `Run ${skill.name}`,
        selected_files: selected,
      }, handleEvent);
    } catch (e) {
      setError(String(e));
      setRunningSkill(null);
    }
  }

  async function createSkill(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setSavingSkill(true);
    try {
      await api.createSkill(pid, draft);
      setDraft(emptyDraft);
      setSkillIdEdited(false);
      setShowCreateForm(false);
      refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSavingSkill(false);
    }
  }

  function toggleDraftTool(name: string) {
    setDraft((current) => ({
      ...current,
      tools: current.tools.includes(name)
        ? current.tools.filter((tool) => tool !== name)
        : [...current.tools, name],
    }));
  }

  function updateSkillName(name: string) {
    setDraft((current) => ({
      ...current,
      name,
      ...(skillIdEdited ? {} : {
        id: name.toLowerCase().normalize("NFKD")
          .replace(/[\u0300-\u036f]/g, "")
          .replace(/[^a-z0-9_-]+/g, "-")
          .replace(/^-+|-+$/g, "")
          .slice(0, 64),
      }),
    }));
  }

  return (
    <div className="chatlog" style={{ overflowY: "auto" }}>
      <div className="mx-auto w-full max-w-4xl space-y-4 p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h1 className="m-0 text-xl font-semibold">Skills</h1>
            <p className="mt-1 text-sm text-muted-foreground">
              Give the cowork agent reusable instructions and project tools for repeatable work.
            </p>
          </div>
          <Button onClick={() => {
            setError("");
            if (showCreateForm) {
              setDraft(emptyDraft);
              setSkillIdEdited(false);
            }
            setShowCreateForm(!showCreateForm);
          }}>
            {showCreateForm ? "Close" : "+ Add skill"}
          </Button>
        </div>

        {showCreateForm && (
          <Card>
            <CardHeader>
              <CardTitle>Create a skill</CardTitle>
              <CardDescription>
                Add reusable instructions. The selected tools limit which project actions the skill can use.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <form className="space-y-4" onSubmit={createSkill}>
                <div className="grid gap-4 sm:grid-cols-2">
                  <label className="space-y-1.5 text-sm font-medium">
                    Skill name
                    <Input
                      value={draft.name}
                      onChange={(e) => updateSkillName(e.target.value)}
                      placeholder="Meeting notes"
                      maxLength={120}
                      required
                    />
                  </label>
                  <label className="space-y-1.5 text-sm font-medium">
                    Skill ID
                    <Input
                      value={draft.id}
                      onChange={(e) => {
                        setSkillIdEdited(true);
                        setDraft((current) => ({ ...current, id: e.target.value }));
                      }}
                      placeholder="meeting-notes"
                      pattern="[a-z0-9][a-z0-9_-]{0,63}"
                      title="Use lowercase letters, numbers, hyphens, or underscores."
                      required
                    />
                  </label>
                </div>

                <label className="block space-y-1.5 text-sm font-medium">
                  Description
                  <Input
                    value={draft.description}
                    onChange={(e) => setDraft((current) => ({ ...current, description: e.target.value }))}
                    placeholder="What should the agent use this skill for?"
                    maxLength={1000}
                  />
                </label>

                <label className="block space-y-1.5 text-sm font-medium">
                  Instructions
                  <Textarea
                    value={draft.prompt}
                    onChange={(e) => setDraft((current) => ({ ...current, prompt: e.target.value }))}
                    placeholder="Describe the steps, output format, and rules the agent should follow."
                    className="min-h-40"
                    maxLength={50000}
                    required
                  />
                </label>

                <fieldset className="space-y-2">
                  <legend className="text-sm font-medium">Allowed tools</legend>
                  <p className="text-xs text-muted-foreground">
                    A skill can only call the tools selected here, and each tool stays limited to this project.
                  </p>
                  {toolCatalog.length > 0 ? (
                    <div className="grid max-h-52 gap-2 overflow-y-auto rounded-lg border p-3 sm:grid-cols-2">
                      {toolCatalog.map((tool) => (
                        <label key={tool.name} className="flex cursor-pointer items-start gap-2 rounded-md p-2 text-sm hover:bg-muted/60">
                          <input
                            type="checkbox"
                            checked={draft.tools.includes(tool.name)}
                            onChange={() => toggleDraftTool(tool.name)}
                            className="mt-1"
                          />
                          <span>
                            <span className="block font-medium">{tool.name}</span>
                            <span className="block text-xs text-muted-foreground">{tool.description}</span>
                          </span>
                        </label>
                      ))}
                    </div>
                  ) : (
                    <p className="rounded-lg border p-3 text-sm text-muted-foreground">
                      Tool list is unavailable. Refresh the page to load available tools.
                    </p>
                  )}
                </fieldset>

                <label className="flex cursor-pointer items-start gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={draft.enabled_for_project}
                    onChange={(e) => setDraft((current) => ({ ...current, enabled_for_project: e.target.checked }))}
                    className="mt-1"
                  />
                  <span>
                    <span className="block font-medium">Enable in this project</span>
                    <span className="block text-xs text-muted-foreground">The skill is saved to the local workspace registry and can be enabled in other projects later.</span>
                  </span>
                </label>

                {error && <p role="alert" className="error-text">{error}</p>}
                <div className="flex justify-end gap-2">
                  <Button type="button" variant="outline" onClick={() => {
                    setDraft(emptyDraft);
                    setSkillIdEdited(false);
                    setError("");
                    setShowCreateForm(false);
                  }}>
                    Cancel
                  </Button>
                  <Button type="submit" disabled={savingSkill || toolCatalog.length === 0}>
                    {savingSkill ? "Adding…" : "Add skill"}
                  </Button>
                </div>
              </form>
            </CardContent>
          </Card>
        )}

        <Card>
          <CardHeader>
            <CardTitle>Available skills</CardTitle>
            <CardDescription>Enable a skill for this project, then run it with an instruction and optional files.</CardDescription>
          </CardHeader>
          <CardContent>
            {skills.length === 0 ? (
              <p className="text-sm text-muted-foreground">No skills are available yet. Add a skill to get started.</p>
            ) : (
              <ul className="m-0 list-none divide-y p-0">
                {skills.map((skill) => (
                  <li key={skill.id} className="flex flex-wrap items-center gap-3 py-3 first:pt-0 last:pb-0">
                    <div className="min-w-48 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <strong>{skill.name}</strong>
                        <Badge variant="secondary">v{skill.version}</Badge>
                        {skill.builtin && <Badge variant="outline">Built in</Badge>}
                      </div>
                      <p className="mb-0 mt-1 text-sm text-muted-foreground">{skill.description || skill.id}</p>
                    </div>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={async () => { await api.toggleSkill(pid, skill.id, !skill.enabled); refresh(); }}
                    >
                      {skill.enabled ? "Disable" : "Enable"}
                    </Button>
                    <Button
                      size="sm"
                      disabled={!skill.enabled || runningSkill !== null}
                      onClick={() => run(skill)}
                    >
                      {runningSkill === skill.id ? "Running…" : "Run"}
                    </Button>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Run a skill</CardTitle>
            <CardDescription>Choose project files as context, then run an enabled skill above.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <label className="block space-y-1.5 text-sm font-medium">
              Instruction
              <Textarea
                value={instruction}
                onChange={(e) => setInstruction(e.target.value)}
                placeholder="Generate requirements from the selected context…"
              />
            </label>
            <div>
              <p className="mb-2 text-sm font-medium">Project files</p>
              <div className="flex flex-wrap gap-2">
                {files.map((file) => {
                  const isSelected = selected.includes(file.name);
                  return (
                    <Button
                      key={file.id}
                      type="button"
                      variant={isSelected ? "secondary" : "outline"}
                      size="sm"
                      aria-pressed={isSelected}
                      onClick={() => setSelected((prev) =>
                        prev.includes(file.name) ? prev.filter((name) => name !== file.name) : [...prev, file.name])}
                    >
                      @{file.name}
                    </Button>
                  );
                })}
                {files.length === 0 && <span className="text-sm text-muted-foreground">No files uploaded yet.</span>}
              </div>
            </div>
            {progress && <p role="status" className="text-sm text-muted-foreground">{progress}</p>}
            {result && <p className="ok-text">{result}</p>}
            {error && !showCreateForm && <p role="alert" className="error-text">{error}</p>}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
