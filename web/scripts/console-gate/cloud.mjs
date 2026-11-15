// Cloud-console gate. The cloud role has ONE page (总览): the status row, the
// one primary action, the Key list, today's usage, the trend and the request
// detail. This gate opens the candidate against the synthetic cloud fixtures and
// reads the rendered page back; it is not a substitute for real
// 18096/Tailscale/UM.
export async function runCloud(c) {
  const rows = [];
  const { page, errors } = await c.newPage({
    width: 1440,
    height: 900,
    ready: ".cloud-console",
  });
  const text = async () =>
    (await page.locator("body").innerText()).replace(/\s+/g, " ");
  const must = (label, condition, detail) => {
    if (!condition) c.fail(label, detail);
    rows.push({ label, ok: Boolean(condition), detail });
  };
  // A2: a failed usage / operations read keeps the last value but marks it
  // stale. The gate answers those reads locally; the submit path itself is
  // checked against the source bundle in scripts/check-cloud-console.mjs.
  const a2State = { failUsage: false, failOps: false };
  const a2Override = async (route, req) => {
    const url = new URL(req.url());
    const path = url.pathname.replace(/^\/gw\/usagesrv/, "");
    if (path === "/api/console/share-usage" && a2State.failUsage) {
      await route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({ error: "fixture_read_failed" }),
      });
      return true;
    }
    if (
      path === "/api/console/operations" &&
      url.searchParams.get("limit") === "8" &&
      a2State.failOps
    ) {
      await route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({ error: "fixture_read_failed" }),
      });
      return true;
    }
    return false;
  };
  const openCloudPage = (label) =>
    c.newPage({
      width: 1440,
      height: 900,
      ready: ".cloud-console",
      settle: 1500,
      override: a2Override,
    });
  const bodyText = async (target) =>
    (await target.locator("body").innerText()).replace(/\s+/g, " ");

  await page.waitForSelector(".cloud-console", { timeout: 20000 });
  const body = await text();

  // Ticket #4: the nav is one item; the four old areas are gone.
  const nav = await page.evaluate(() =>
    [...document.querySelectorAll(".shell-nav [data-cloud-area]")].map(
      (node) =>
        node.querySelector(".shell-nav-text")?.textContent?.trim() ?? ""
    )
  );
  must(
    "cloud-nav",
    nav.length === 1 && nav[0] === "总览",
    `导航不是只有总览：${nav.join(" / ") || "空"}`
  );
  must(
    "cloud-nav-old-areas",
    (await page.locator(
      '[data-cloud-area="keys"], [data-cloud-area="usage"], [data-cloud-area="services"], [data-cloud-area="ops"]'
    ).count()) === 0,
    "旧导航项仍在页面里"
  );

  // Ticket #5: the status row shows the display state, its read time and the
  // login-window actions that follow that state.
  const status = await page.evaluate(() => ({
    word:
      document.querySelector("[data-cloud-status-toggle]")?.textContent?.trim() ??
      "",
    time:
      document.querySelector(".cloud-status-time")?.textContent?.trim() ?? "",
    login: [...document.querySelectorAll(".cloud-status-login button")].map(
      (node) => node.textContent?.trim() ?? ""
    ),
  }));
  must(
    "cloud-status-word",
    status.word === "登录中",
    `状态词不是读面确认的登录中：${status.word}`
  );
  must(
    "cloud-status-time",
    /^数据更新于 \d{2}:\d{2}$/.test(status.time),
    `状态行没有数据更新时间：${status.time}`
  );
  must(
    "cloud-status-login",
    status.login.includes("打开登录窗口") && status.login.includes("取消登录"),
    `登录中右侧动作不完整：${status.login.join(" / ")}`
  );

  // Ticket #6: one primary action, and the fixture's gateway is running.
  must(
    "cloud-primary-single",
    (await page.locator("[data-cloud-primary]").count()) === 1,
    "主动作按钮不是唯一一个"
  );
  must(
    "cloud-primary-stop",
    ((await page.locator("[data-cloud-primary]").innerText()) ?? "").trim() ===
      "停止",
    "运行中的主动作不是停止"
  );
  for (const word of ["一键启动", "普通停止", "强制停止"])
    must(
      `cloud-primary-old:${word}`,
      !body.includes(word),
      `页面仍有旧按钮 ${word}`
    );

  // Ticket #7/#20: only active Keys render; a revoked Key is gone and the page
  // never shows 已停用 or an enable/disable toggle.
  const keyRows = await page.evaluate(() =>
    [...document.querySelectorAll("[data-cloud-key-row]")].map((row) => ({
      id: row.getAttribute("data-cloud-key-row") ?? "",
      text: row.innerText.replace(/\s+/g, " "),
    }))
  );
  must(
    "cloud-keys",
    keyRows.some((row) => row.text.includes("alice-laptop")) &&
      keyRows.some((row) => row.text.includes("bob-cursor")),
    "Key 列表没有渲染夹具中的有效 Key"
  );
  must(
    "cloud-keys-revoked-absent",
    !keyRows.some((row) => row.text.includes("carol-test")) &&
      !body.includes("已停用"),
    "已撤销 Key 仍出现在列表或显示已停用"
  );
  must(
    "cloud-keys-no-toggle",
    !body.includes("启用后才能测试") &&
      (await page.locator('[data-cloud-key-action^="toggle:"]').count()) === 0,
    "Key 列表仍提供停用/启用"
  );
  must(
    "cloud-key-concurrency",
    body.includes("并发 3 / 64"),
    `Key 列表头部没有全局并发读数：${body.slice(0, 200)}`
  );
  const keyRow = await page.evaluate(
    () =>
      [...document.querySelectorAll("[data-cloud-key-row]")]
        .find((row) => row.innerText.includes("alice-laptop"))
        ?.innerText.replace(/\s+/g, " ") ?? ""
  );
  must(
    "cloud-key-row",
    keyRow.includes("um-share-****Qm7x") &&
      keyRow.includes("撤销") &&
      (await page.locator("[data-cloud-key-cap-input]").count()) > 0,
    `Key 行缺少掩码/并发/撤销：${keyRow.slice(0, 200)}`
  );

  // Ticket #8/#9: today's figures and the request detail are real reads.
  const figures = await page.evaluate(() =>
    Object.fromEntries(
      [...document.querySelectorAll("[data-cloud-usage-figure]")].map((node) => [
        node.getAttribute("data-cloud-usage-figure") ?? "",
        node.querySelector("[data-cloud-usage-value]")?.textContent?.trim() ?? "",
      ])
    )
  );
  must(
    "cloud-today-requests",
    Boolean(figures.requests) &&
      !/未知|读取中|后端未提供/.test(figures.requests),
    `今日请求没有读到后端数值：${figures.requests ?? "缺失"}`
  );
  must(
    "cloud-trend",
    (await page.locator("svg.cloud-chart").count()) === 1,
    "单页缺少 14 天趋势图"
  );
  await page.waitForSelector("[data-cloud-request-row]", { timeout: 10000 });
  const requestRows = await page.evaluate(() =>
    [...document.querySelectorAll("[data-cloud-request-row]")].map((row) =>
      row.innerText.replace(/\s+/g, " ")
    )
  );
  must(
    "cloud-requests",
    requestRows.some((row) => row.includes("朋友 A")) &&
      requestRows.some((row) => row.includes("朋友 B")),
    "请求明细没有读到夹具行"
  );
  must(
    "cloud-requests-no-loading",
    !body.includes("请求明细读取中") &&
      !body.includes("当前筛选没有请求") &&
      !body.includes("读取失败"),
    "请求明细被渲染成读取中/失败/空结果"
  );

  // Ticket #5: the expanded status row has four evidence lines and the two
  // read-only actions; the read-only check is not described as a change.
  await page.locator("[data-cloud-status-toggle]").click();
  const detail = await page.locator("[data-cloud-status-detail]").innerText();
  for (const evidence of [
    "认证读数",
    "上游认证",
    "最近核验",
    "登录动作",
    "分层检查（只读）",
    "重启分享入口 8100",
  ])
    must(
      `cloud-status-detail:${evidence}`,
      detail.includes(evidence),
      `展开详情缺少 ${evidence}`
    );

  // A2: a failed usage read keeps the last value but marks it stale; the old
  // number is never presented as current usage.
  const staleUsage = await openCloudPage("cloud-stale-usage");
  try {
    a2State.failUsage = true;
    await staleUsage.page.locator("[data-cloud-refresh]").first().click();
    await staleUsage.page
      .waitForFunction(
        () => document.body.innerText.includes("用量读数已过时"),
        null,
        { timeout: 5000 }
      )
      .catch(() => {});
    const staleText = await bodyText(staleUsage.page);
    must(
      "cloud-stale-usage",
      staleText.includes("用量读数已过时") &&
        staleText.includes("今日用量未知"),
      `用量读取失败后未标过时：${staleText.slice(0, 240)}`
    );
    const staleFigures = await staleUsage.page.evaluate(() =>
      [...document.querySelectorAll("[data-cloud-usage-value]")].map(
        (node) => node.textContent?.trim() ?? ""
      )
    );
    must(
      "cloud-stale-usage-unknown",
      staleFigures.includes("未知"),
      `用量读取失败后仍把上次读数当当前值：${staleFigures.join(" / ")}`
    );
    must(
      "cloud-stale-usage-errors",
      staleUsage.errors.length === 0,
      staleUsage.errors.join("；")
    );
  } finally {
    a2State.failUsage = false;
    await staleUsage.page.context().close();
  }

  // A2: a failed operations read keeps the last evidence but marks it stale;
  // the old observation is not a live green or a current "not read".
  const staleOps = await openCloudPage("cloud-stale-ops");
  try {
    await staleOps.page
      .waitForFunction(
        () => /认证有效|UM 认证/.test(document.body.innerText),
        null,
        { timeout: 8000 }
      )
      .catch(() => {});
    a2State.failOps = true;
    await staleOps.page.locator("[data-cloud-refresh]").first().click();
    await staleOps.page
      .waitForFunction(
        () => document.body.innerText.includes("读数已过时"),
        null,
        { timeout: 5000 }
      )
      .catch(() => {});
    await staleOps.page.locator("[data-cloud-status-toggle]").click();
    const staleDetail = await staleOps.page
      .locator("[data-cloud-status-detail]")
      .innerText();
    must(
      "cloud-stale-ops",
      staleDetail.includes("读数已过时") &&
        staleDetail.includes("操作读数已过时") &&
        !staleDetail.includes("未读取到最近一次分层检查"),
      `操作读数失败后未标过时：${staleDetail.slice(0, 300)}`
    );
    must(
      "cloud-stale-ops-errors",
      staleOps.errors.length === 0,
      staleOps.errors.join("；")
    );
  } finally {
    a2State.failOps = false;
    await staleOps.page.context().close();
  }

  // A1: a login trigger renders 已触发/等待本人登录, never 已完成.
  const loginPage = await c.newPage({
    width: 1440,
    height: 900,
    ready: ".cloud-console",
    override: async (route, req) => {
      const url = new URL(req.url());
      if (url.pathname !== "/gw/usagesrv/api/console/operations") return false;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          operations: [
            {
              id: "fx-cloud-op-login-1",
              action: "account.login",
              target: "share",
              state: "succeeded",
              created_at: 1793413300,
              finished_at: 1793413301,
              result: {
                triggered: true,
                state: "logging_in",
                label: "登录进行中",
                started_here: true,
                restored: null,
                note: "登录进行中——请在弹出的专用实例浏览器窗口完成 UM 登录",
              },
              error: null,
              steps: [{ at: 1793413301, note: "登录触发已受理" }],
              idempotency_key: "idem-gate-login",
            },
          ],
        }),
      });
      return true;
    },
  });
  try {
    await loginPage.page.locator("[data-cloud-status-toggle]").click();
    const loginDetail = await loginPage.page
      .locator("[data-cloud-status-detail]")
      .innerText();
    must(
      "cloud-login-triggered",
      loginDetail.includes("登录动作") &&
        loginDetail.includes("已触发 · 等待本人登录") &&
        !/登录动作[^]{0,80}已完成/.test(loginDetail),
      `登录动作没有如实显示已触发/等待本人登录：${loginDetail.slice(0, 200)}`
    );
    must(
      "cloud-login-errors",
      loginPage.errors.length === 0,
      loginPage.errors.join("；")
    );
  } finally {
    await loginPage.ctx.close();
  }

  must("cloud-page-errors", errors.length === 0, errors.join("；"));

  // The single page keeps a usable mobile layout and the drawer still opens.
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(400);
  const mobile = await page.evaluate(() => ({
    toggle: Boolean(document.querySelector("[data-cloud-drawer-toggle]")),
    overflow: document.documentElement.scrollWidth > window.innerWidth + 2,
  }));
  must("cloud-mobile-nav", mobile.toggle, "手机宽度没有导航开关");
  must("cloud-mobile-overflow", !mobile.overflow, "手机宽度出现页面级横向溢出");
  await page.screenshot({ path: c.shot("cloud-mobile-390"), fullPage: true });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.waitForTimeout(300);
  await page.screenshot({ path: c.shot("cloud-desktop"), fullPage: true });
  return { rows, ok: rows.every((row) => row.ok) };
}
