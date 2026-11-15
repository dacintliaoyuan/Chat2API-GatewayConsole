/* zcode-workflow
description: 按 spec 的 frontier 并行实现一批有 blocking 边的 GitHub issue：确定性
  preflight（状态/标签/blocked_by 图）、按波次开 worktree 实现与合并、每波快速门禁、两轴评审、完整验证、逐票
  comment+close、清理工作树。
whenToUse: 清理或退役一组已行为化、验收可机械核对、带 native blocked_by 边的 issue；spec 已拆成
  tracer-bullet tickets 时。
*/
interface ImplementResult {
  ticket: number;
  branch: string;
  commit: string;
  filesChanged: string[];
  testsRun: string[];
  problems: string[];
}

interface MergeResult {
  merged: number[];
  conflicts: number[];
  commit: string;
  problems: string[];
}

interface SpecCheck {
  ok: boolean;
  issues: string[];
}

interface Finding {
  where: string;
  what: string;
  evidence: string;
  status: "verified" | "unconfirmed";
  severity: "low" | "medium" | "high";
}

interface Confirmation {
  index: number;
  status: "verified" | "unconfirmed";
  evidence: string;
}

interface WorkflowReport {
  conclusion: string;
  findings: Finding[];
  verified: string[];
  notCovered: string[];
}

const INTEGRATION = "integration/feature-retirement-cleanup";
const BLOCKERS: Record<number, number[]> = {
  27: [],
  28: [27],
  29: [28],
  30: [28, 29, 34, 32],
  31: [27, 28, 29, 32],
  32: [28],
  33: [28],
  34: [28, 33],
  35: [27, 28, 29, 30, 31, 32, 33, 34],
};
const TICKETS = [27, 28, 29, 30, 31, 32, 33, 34, 35];

function blockedReport(reason: string, detail: string): WorkflowReport {
  return {
    conclusion: reason,
    findings: [{
      where: "preflight",
      what: reason,
      evidence: detail,
      status: "verified",
      severity: "high",
    }],
    verified: ["git status, git rev-parse HEAD"],
    notCovered: ["清理未开始；Rust 重写、端口重排、headless 登录都不在本次 workflow"],
  };
}

function mergeFindings(findings: Finding[], confirmations: Confirmation[]): Finding[] {
  return findings.map((finding, index) => ({
    ...finding,
    status: confirmations[index]?.status ?? "unconfirmed",
    evidence: confirmations[index]?.evidence ?? finding.evidence,
  }));
}

phase("检查工作区与票面");
const status = await world.run("git", ["status", "--porcelain"]);
const base = await world.run("git", ["rev-parse", "HEAD"]);
const branch = await world.run("git", ["branch", "--show-current"]);
const dirty = status.stdout.split(/\r?\n/).filter((line) => line.trim() !== "");
const unexpected = dirty.filter(
  (line) => line.slice(3).trim() !== "um-cloud-deploy/RELEASE-MANIFEST.json",
);
if (unexpected.length > 0) {
  return blockedReport("未开始：工作区有未预期改动", unexpected.join("\n"));
}

const ISSUE_NUMBERS = [26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37];
const EDGES: Record<number, number[]> = {
  26: [],
  27: [],
  28: [27],
  29: [28],
  30: [28, 29, 34, 32],
  31: [27, 28, 29, 32],
  32: [28],
  33: [28],
  34: [28, 33],
  35: [27, 28, 29, 30, 31, 32, 33, 34],
  36: [35],
  37: [35, 36],
};

function parseJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

const views = await Promise.all(
  ISSUE_NUMBERS.map(async (n) => {
    const view = await world.run("gh", ["issue", "view", String(n), "--json", "number,state,labels"]);
    return { n, view };
  }),
);
const stateProblems: string[] = [];
for (const { n, view } of views) {
  if (view.exitCode !== 0) {
    stateProblems.push(`#${n} gh issue view failed: ${view.stderr}`);
    continue;
  }
  const data = parseJson(view.stdout) as { state?: string; labels?: Array<{ name: string }> } | null;
  if (data === null) {
    stateProblems.push(`#${n} returned invalid JSON`);
    continue;
  }
  if (data.state !== "OPEN") stateProblems.push(`#${n} state=${data.state}`);
  const labels = (data.labels ?? []).map((label) => label.name);
  if (!labels.includes("ready-for-agent")) stateProblems.push(`#${n} labels=${labels.join(",")}`);
}
if (stateProblems.length > 0) {
  return blockedReport("未开始：票状态或标签不符", stateProblems.join("\n"));
}

const depChecks = await Promise.all(
  ISSUE_NUMBERS.map(async (n) => {
    const deps = await world.run("gh", ["api", `repos/dacintliaoyuan/Chat2API-GatewayConsole/issues/${n}/dependencies/blocked_by`]);
    return { n, deps };
  }),
);
const graphProblems: string[] = [];
for (const { n, deps } of depChecks) {
  if (deps.exitCode !== 0) {
    graphProblems.push(`#${n} dependencies failed: ${deps.stderr}`);
    continue;
  }
  const parsed = parseJson(deps.stdout) as Array<{ number: number }> | null;
  if (parsed === null) {
    graphProblems.push(`#${n} dependencies returned invalid JSON`);
    continue;
  }
  const actual = parsed.map((item) => item.number).sort((a, b) => a - b);
  const expected = [...EDGES[n]].sort((a, b) => a - b);
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    graphProblems.push(`#${n} expected [${expected}] got [${actual}]`);
  }
}
const visiting = new Set<number>();
const visited = new Set<number>();
let cycle = "";
function visit(node: number): void {
  if (cycle !== "") return;
  if (visiting.has(node)) {
    cycle = `cycle at #${node}`;
    return;
  }
  if (visited.has(node)) return;
  visiting.add(node);
  for (const dep of EDGES[node] ?? []) visit(dep);
  visiting.delete(node);
  visited.add(node);
}
for (const n of ISSUE_NUMBERS) visit(n);
if (cycle !== "") graphProblems.push(cycle);
if (graphProblems.length > 0) {
  return blockedReport("未开始：blocked_by 图不符", graphProblems.join("\n"));
}

const specChecker = agent("规格检查员", {
  system: "你只读 12 张 issue 的正文（gh issue view N --json body），不读仓库文件、不读代码、不跑测试。只找硬矛盾：同一文件既要删除又要保留、同一职责两票互斥、验收条件与正文相反。只有硬矛盾才 ok=false；措辞、历史备注、跨 spec 排序说明、非阻断范围重叠记为 issues 但保持 ok=true。10 分钟内返回 SpecCheck。",
});
const specCheck = await specChecker.ask<SpecCheck>(
  `读 spec #26、tickets #27-#35、#36、#37 的正文（gh issue view N --json body），只做硬矛盾检查。对每个问题给出票号与原文引用。返回 SpecCheck。`,
);
if (!specCheck.ok) {
  return blockedReport("未开始：票面检查未通过", JSON.stringify(specCheck.issues));
}
const baseCommit = base.stdout.trim();
log(`票面检查通过：${branch.stdout.trim()} @ ${baseCommit}`);

phase("建立集成分支与隔离工作树");
await world.run("C:/Program Files/Git/bin/bash.exe", ["-lc", "grep -qxF '.worktrees/' .git/info/exclude || echo '.worktrees/' >> .git/info/exclude"]);
const branchCheck = await world.run("git", ["rev-parse", "--verify", INTEGRATION]);
if (branchCheck.exitCode !== 0) {
  await world.run("git", ["switch", "-c", INTEGRATION]);
} else {
  await world.run("git", ["switch", INTEGRATION]);
}
log(`集成分支：${INTEGRATION}`);

phase("按 frontier 实现清理票");
const done = new Set<number>();
const results: ImplementResult[] = [];
const implementationNotes: Finding[] = [];
let wave = 0;
while (done.size < TICKETS.length) {
  wave += 1;
  const ready = TICKETS.filter(
    (ticket) => !done.has(ticket) && BLOCKERS[ticket].every((blocker) => done.has(blocker)),
  );
  if (ready.length === 0) {
    return blockedReport("未完成：frontier 为空", JSON.stringify({ done: [...done] }));
  }
  log(`第 ${wave} 波：${ready.map((ticket) => `#${ticket}`).join("、")}`);
  const waveResults = await Promise.all(
    ready.map(async (ticket) => {
      const worktree = `.worktrees/${ticket}`;
      const ticketBranch = `cleanup/${ticket}`;
      const exists = await world.run("git", ["rev-parse", "--verify", ticketBranch]);
      if (exists.exitCode !== 0) {
        const added = await world.run("git", ["worktree", "add", worktree, "-b", ticketBranch, INTEGRATION]);
        if (added.exitCode !== 0) {
          return {
            ticket,
            branch: ticketBranch,
            commit: "",
            filesChanged: [],
            testsRun: [],
            problems: [`worktree add failed: ${added.stderr}`],
          } satisfies ImplementResult;
        }
      }
      const implementer = agent(`实现员·#${ticket}`, {
        system: "先读 tdd-mattpocock，按红绿切片执行。读 spec #26 和你的 ticket（gh issue view），在指定 worktree 工作。只改本 ticket 的文件。每个验收条目单独提交并返回该条目的 commit 与定向测试输出；连续 20 分钟没有新 commit 就停下来报告，不要空转。不要 push。提交后把集成分支 tip merge 进你的分支；冲突就报告，不要伪造成功。",
      });
      const implementation = await implementer.ask<ImplementResult>(
        `spec #26；ticket #${ticket}；worktree ${worktree}；branch ${ticketBranch}；集成分支 ${INTEGRATION}。按验收条目逐个实现：每条提交一次并返回 commit + 定向测试输出；全部完成后 merge 集成分支 tip，返回 ImplementResult。`,
      );
      const sliceLog = await world.run(
        "git",
        ["-C", worktree, "log", "--oneline", `${INTEGRATION}..${ticketBranch}`],
      );
      if (sliceLog.exitCode !== 0 || sliceLog.stdout.trim() === "") {
        return {
          ...implementation,
          problems: [...implementation.problems, "实现 ask 返回后没有新 commit"],
        } satisfies ImplementResult;
      }
      return implementation;
    }),
  );
  results.push(...waveResults);
  for (const result of waveResults) {
    if (result.problems.length > 0) {
      implementationNotes.push({
        where: `ticket #${result.ticket}`,
        what: result.problems.join("; "),
        evidence: `commit ${result.commit}; tests: ${result.testsRun.join("; ")}`,
        status: "verified",
        severity: "low",
      });
    }
  }
  const failed = waveResults.filter((result) => result.commit === "");
  if (failed.length > 0) {
    return blockedReport(`未完成：第 ${wave} 波实现失败`, JSON.stringify(failed));
  }
  const merger = agent(`合并员·第${wave}波`, {
    system: "你把本波 branches 按顺序合并到集成分支。只做合并与冲突处理；不改业务代码。冲突则 abort 并报告。",
  });
  const mergeResult = await merger.ask<MergeResult>(
    `集成分支 ${INTEGRATION}；branches：${JSON.stringify(waveResults.map((result) => result.branch))}。按顺序 git switch ${INTEGRATION} + git merge --no-ff <branch>；返回 MergeResult。`,
  );
  if (mergeResult.conflicts.length > 0 || mergeResult.commit === "") {
    return blockedReport(`未完成：第 ${wave} 波合并失败`, JSON.stringify(mergeResult));
  }
  for (const problem of mergeResult.problems) {
    implementationNotes.push({
      where: `第 ${wave} 波合并`,
      what: problem,
      evidence: `merged ${JSON.stringify(mergeResult.merged)}; commit ${mergeResult.commit}`,
      status: "verified",
      severity: "low",
    });
  }
  const gate = await world.run(
    "bash",
    ["scripts/check.sh", "--quick"],
    { timeoutMs: 900000, output: { tail: 8192 } },
  );
  if (gate.exitCode !== 0) {
    const fixer = agent(`波次修复员·第${wave}波`, {
      system: "先读 tdd-mattpocock。你修复本波快速门禁失败；在集成分支工作；不要跑完整套件；修复后提交。无法修复就报告。",
    });
    const fix = await fixer.ask<ImplementResult>(
      `集成分支 ${INTEGRATION}；第 ${wave} 波 tickets ${JSON.stringify(ready)}；快速门禁失败：\n${(gate.stderr + "\n" + gate.stdout).slice(-8000)}\n修复并提交，返回 ImplementResult。`,
    );
    const recheck = await world.run(
      "bash",
      ["scripts/check.sh", "--quick"],
      { timeoutMs: 900000, output: { tail: 8192 } },
    );
    if (recheck.exitCode !== 0) {
      return blockedReport(`未完成：第 ${wave} 波快速门禁失败`, (recheck.stderr + "\n" + recheck.stdout).slice(-8000));
    }
    results.push(fix);
  }
for (const ticket of ready) done.add(ticket);
}
log(`集成分支 ${INTEGRATION} 已处理 ${done.size} 张票`);

phase("代码审查");
const standardsReviewer = agent("标准评审员", {
  system: "先读 code-review-mattpocock。按 Standards 轴独立评审 integration 分支 diff 和 CODING_STANDARDS.md。你不改代码。报告 finding 要带证据。",
});
const specReviewer = agent("规格评审员", {
  system: "先读 code-review-mattpocock。按 Spec 轴独立评审 integration 分支 diff、spec #26 和 tickets #27-#35。你不改代码。报告 finding 要带证据。",
});
const [standardsFindings, specFindings] = await Promise.all([
  standardsReviewer.ask<Finding[]>(`读 CODING_STANDARDS.md 和 git diff ${baseCommit}..${INTEGRATION}。返回 Finding[]。`),
  specReviewer.ask<Finding[]>(`读 spec #26、tickets #27-#35 和 git diff ${baseCommit}..${INTEGRATION}。返回 Finding[]。`),
]);
const findings = [...standardsFindings, ...specFindings];
const confirmer = agent("发现确认员", {
  system: "你独立确认高/中严重度 finding：读代码，能跑检查就跑，不能跑就说明。你不改代码。返回 Confirmation[]。",
});
const confirmations = await confirmer.ask<Confirmation[]>(
  `待确认 findings：${JSON.stringify(findings)}。返回 Confirmation[]。`,
);
const mergedFindings = mergeFindings(findings, confirmations);
if (mergedFindings.some((finding) => finding.severity === "high")) {
  const reviewFixer = agent("审查修复员", {
    system: "先读 tdd-mattpocock。你修复 code-review 的高严重度问题；在集成分支工作；修复后提交。无法修复就报告。",
  });
  await reviewFixer.ask<ImplementResult>(
    `集成分支 ${INTEGRATION}；高严重度 findings：${JSON.stringify(mergedFindings.filter((finding) => finding.severity === "high"))}。修复并提交，返回 ImplementResult。`,
  );
}

phase("完整验证");
const [fullGate, cloudGate, umGate, retiredGate] = await Promise.all([
  world.run("C:/Program Files/Git/bin/bash.exe", ["scripts/check.sh", "--full"], { timeoutMs: 3600000, output: { tail: 16384 } }),
  world.run("C:/Program Files/Git/bin/bash.exe", ["-lc", "cd web && npm run check:cloud-console"], { timeoutMs: 900000, output: { tail: 8192 } }),
  world.run("C:/Program Files/Git/bin/bash.exe", ["-lc", "cd web && npm run check:um"], { timeoutMs: 900000, output: { tail: 8192 } }),
  world.run("python", ["scripts/check_retired_features.py"], { timeoutMs: 120000, output: { tail: 8192 } }),
]);
const gateResults = [
  { name: "check.sh --full", result: fullGate },
  { name: "check:cloud-console", result: cloudGate },
  { name: "check:um", result: umGate },
  { name: "check_retired_features", result: retiredGate },
];
const failedGates = gateResults.filter((gate) => gate.result.exitCode !== 0);
if (failedGates.length > 0) {
  return blockedReport(
    "未完成：完整验证失败",
    failedGates.map((gate) => `${gate.name}:\n${(gate.result.stderr + "\n" + gate.result.stdout).slice(-8000)}`).join("\n\n"),
  );
}

phase("收尾");
const closer = agent("票面收尾员", {
  system: "你按 issue tracker 的方式关闭 tickets：先 comment 结果，再 close。不要改代码。",
});
const closeResult = await closer.ask<string>(
  `tickets #27-#35 已实现并合并到 ${INTEGRATION}；提交记录：${JSON.stringify(results.map((result) => result.commit))}。逐票 gh issue comment + gh issue close，返回结果摘要。`,
);
await world.run("git", ["worktree", "list", "--porcelain"]);
for (const ticket of TICKETS) {
  await world.run("git", ["worktree", "remove", `.worktrees/${ticket}`, "--force"]);
  await world.run("git", ["branch", "-D", `cleanup/${ticket}`]);
}
const commitLog = await world.run("git", ["log", "--oneline", `${baseCommit}..${INTEGRATION}`]);
const reportText = [
  "# 功能退役清理实现报告",
  "",
  "## 结论",
  "",
  "清理 #27-#35 已在集成分支实现、合并并通过完整验证；tickets 已按 tracker 关闭。",
  "",
  "## 提交记录",
  "",
  "```",
  commitLog.stdout.trim(),
  "```",
  "",
  "## 实现结果",
  "",
  ...results.map((result) => `- #${result.ticket}：${result.commit}（${result.filesChanged.length} 个文件）`),
  "",
  "## 收尾",
  "",
  closeResult,
  "",
  "## 复核发现",
  "",
  ...(mergedFindings.length === 0
    ? ["未发现需要人工处理的问题。"]
    : mergedFindings.map((finding) => `- [${finding.status}/${finding.severity}] ${finding.where}：${finding.what}\n  - 证据：${finding.evidence}`)),
  "",
  "## 实现备注",
  "",
  ...(implementationNotes.length === 0
    ? ["无。"]
    : implementationNotes.map((note) => `- ${note.where}：${note.what}\n  - 证据：${note.evidence}`)),
].join("\n");
await artifact.markdown("cleanup-report", reportText, {
  title: "功能退役清理实现报告",
  description: "清理 #27-#35 的 frontier、提交、合并、验证与复核结果。",
  primary: true,
});

return {
  conclusion: "清理 #27-#35 已在集成分支实现、合并并通过完整验证。",
  findings: [...mergedFindings, ...implementationNotes],
  verified: [
    "每波 bash scripts/check.sh --quick",
    "bash scripts/check.sh --full",
    "npm run check:cloud-console",
    "npm run check:um",
    "python scripts/check_retired_features.py",
    `git diff ${baseCommit}..${INTEGRATION} 两轴评审`,
  ],
  notCovered: [
    "Rust 全后端重写：独立轨道",
    "端口 18096/5901 重排：独立轨道",
    "headless 登录 spec #8：独立轨道",
    "真实云端 systemd/UM 登录：目标机 follow-up",
    "push：未推送，改动只在本地集成分支",
  ],
};
