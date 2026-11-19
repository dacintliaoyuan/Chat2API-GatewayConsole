# Rust 浏览器自动化 crate 调研：用高层库替换手写 CDP 驱动

> 调研日期：2026-11-19。版本、下载量、issue 数是当天的快照，之后会变化。
>
> 范围：优先回答“纯 Rust 的 CDP 客户端里，谁能提供 Playwright 式的 Locator、click、fill、actionability 等待和自动滚动”。Playwright 绑定（需要 Node driver）和 WebDriver 封装（需要 chromedriver/geckodriver/msedgedriver）只在第 5 节作为“为什么不选”出现。
>
> 方法：crates.io API 与 docs.rs 取版本/许可证/MSRV；GitHub 源码与 commit 取实现细节；Playwright 官方文档取基准语义。文中每个判断尽量给出 URL，源码判断尽量给到文件与行号。

## 0. 结论

按“纯 Rust + CDP + 尽量接近 Playwright 语义”排序：

1. **`chromiumoxide` 0.9.1**：最成熟、下载量最大、仍在维护的纯 Rust CDP 客户端。Edge 检测、持久 profile、headless 都有现成支持；但它的 `click` 只是“滚动到视口 + 在元素中心发鼠标事件”，没有 Locator、没有 actionability 等待、没有重试、没有 `fill`。选它意味着保留“自己补 Playwright 语义”的工作，但比现在的手写 WebSocket 客户端省掉协议与启动细节。
2. **`headless_chrome` 1.0.22**：同样是成熟的纯 Rust CDP 客户端，同步 API，Edge 检测和持久 profile 都支持；`click`/`type_into` 的语义与 `chromiumoxide` 几乎相同（滚动 + 中心点鼠标事件），同样没有 actionability 和重试。只有旧 `--headless`，没有 `--headless=new`。
3. **`ferridriver` 0.7.0**：本次调研中唯一把 Playwright 的 Locator、visible/enabled/stable、hit-target、滚动对齐、重试和 `fill` 的 editable 检查都实现出来的纯 Rust CDP 库。语义最接近，但仍是 pre-1.0（0.7.0，2026-11-15），只有 6 个 star、约 992 次下载，Windows/Edge 没有文档化验证，不能直接当作生产依赖。

如果要求“今天就能稳定替换”，建议 `chromiumoxide` + 在仓库内补一个小 actionability 层；如果愿意为语义一致性承担 pre-1.0 风险，`ferridriver` 是唯一值得做 spike 的候选。`rustwright` 的原生 Rust API 也实现了 actionability，但它是选择器 API 而不是 Locator，且明确 Alpha、Chromium-only。

---

## 1. 现有驱动与目标语义

当前 `rust/crates/gateway/src/login/browser.rs` 的 `LoginPage` trait 已经抽象出登录状态机需要的动作：

- `fn click(&self, target: &VisibleTarget)` 和 `fn fill(&self, selectors: &[&str], value: &str)` 在 trait 中声明（[browser.rs:96-120](https://github.com/dacintliaoyuan/Chat2API-GatewayConsole/blob/main/rust/crates/gateway/src/login/browser.rs#L96-L120)）。
- 真实实现自己做了 `CLICK_ATTEMPTS = 3`、`CLICK_ACTIONABILITY_BUDGET = 2s`、`CLICK_ACTIONABILITY_POLL = 50ms`（[browser.rs:28-31](https://github.com/dacintliaoyuan/Chat2API-GatewayConsole/blob/main/rust/crates/gateway/src/login/browser.rs#L28-L31)）。
- `click_scroll_expression` 调用 `scrollIntoView({block:'center',inline:'center',behavior:'instant'})`，`click_actionability_expression` 用 `getBoundingClientRect()` 和 `document.elementFromPoint(x,y)` 做 hit test（[browser.rs:922-954](https://github.com/dacintliaoyuan/Chat2API-GatewayConsole/blob/main/rust/crates/gateway/src/login/browser.rs#L922-L954)）。

也就是说，现有驱动已经在手写 Playwright 的一部分语义：滚动、2 秒 actionability 预算、hit test、3 次固定点击重试。但它是仓库自己维护的 CDP 子集，缺少 Playwright 的 stable（两帧 bounding box 不变）、enabled/editable、导航等待，以及到 timeout 为止的持续重试（现有只有 3 次固定尝试）。调研的目标是找一个现成库，把这些语义从仓库里拿出去。

---

## 2. Playwright 的基准语义

后面的对比都以 Playwright 官方文档为准。

### 2.1 actionability 检查

Playwright 在动作前会做这些检查（[Actionability](https://playwright.dev/docs/actionability)）：

- **Visible**：“Element is considered visible when it has non-empty bounding box and does not have `visibility:hidden` computed style.”
- **Stable**：“Element is considered stable when it has maintained the same bounding box for at least two consecutive animation frames.”
- **Receives Events**：“Element is considered receiving pointer events when it is the hit target of the pointer event at the action point.”
- **Enabled**：“Element is considered enabled when it is not disabled.”
- **Editable**：“Element is considered editable when it is enabled and is not readonly.”
- **Not Inert**：“Element is considered not inert when it is not a part of an inert subtree.”
- 对 click 还要求：“locator resolves to exactly one element.”
- 时间语义：“It auto-waits for all the relevant checks to pass and only then performs the requested action.”；“If the required checks do not pass within the given timeout, action fails with the `TimeoutError`.”

### 2.2 `locator.click()`

官方文档写明的步骤（[Locator.click](https://playwright.dev/docs/api/class-locator#locator-click)）：

- “Wait for actionability checks on the element, unless `force` option is set.”
- “Scroll the element into view if needed.”
- “Use `page.mouse` to click in the center of the element, or the specified position.”
- “Wait for initiated navigations to either succeed or fail, unless `noWaitAfter` option is set.”
- “If the element is detached from the DOM at any moment during the action, this method throws.”
- `force` 默认 `false`，`trial` 只做检查不执行动作，`timeout` 默认 0（不超时）。

### 2.3 `locator.fill()`

官方文档写明（[Locator.fill](https://playwright.dev/docs/api/class-locator#locator-fill)）：它“waits for actionability checks, focuses the element, fills it and triggers an input event after filling.”；空字符串会清空；目标不是 input/textarea/contenteditable 时抛错；逐字符输入用 `pressSequentially()`。

### 2.4 `locator.wait_for()`

官方文档写明（[Locator.waitFor](https://playwright.dev/docs/api/class-locator#locator-wait-for)）：“Returns when element specified by locator satisfies the `state` option.”；如果已经满足条件立即返回，否则“waits for up to `timeout` milliseconds until the condition is met”；`state` 可为 `attached`、`detached`、`visible`、`hidden`，默认 `visible`。

### 2.5 Locator 的整体定位

官方文档（[Locators](https://playwright.dev/docs/locators)）：“Locators are the central piece of Playwright's auto-waiting and retry-ability.”；“Every time a locator is used for an action, an up-to-date DOM element is located in the page.” 这两句是判断一个 Rust 库是否真的“Playwright-like”的核心标准：不是有没有 `click(selector)`，而是动作前是否重新解析、等待、重试。

---

## 3. 纯 Rust CDP 候选逐个对比

### 3.1 `chromiumoxide` 0.9.1

#### 版本、维护、许可证

| 项目 | 值 | 依据 |
|---|---|---|
| 最新版本 / 发布日期 | 0.9.1 / 2026-02-25 | [crates.io API](https://crates.io/api/v1/crates/chromiumoxide) |
| 下载量（总 / 近期） | 6,029,510 / 2,502,697 | [crates.io API](https://crates.io/api/v1/crates/chromiumoxide) |
| 许可证 | MIT OR Apache-2.0 | [Cargo.toml:8](https://github.com/mattsse/chromiumoxide/blob/main/Cargo.toml#L8) |
| MSRV | 1.85 | [Cargo.toml:4](https://github.com/mattsse/chromiumoxide/blob/main/Cargo.toml#L4)；[crates.io API](https://crates.io/api/v1/crates/chromiumoxide) |
| Cargo features | `fetcher`、`rustls` / `native-tls`、`zip0` / `zip8` | [Cargo.toml:54-63](https://github.com/mattsse/chromiumoxide/blob/main/Cargo.toml#L54-L63) |
| 最后提交 | `afcc3a4`，2026-04-03 | [commit](https://github.com/mattsse/chromiumoxide/commit/afcc3a4313f2087249b4490d94e54bf8e3bfaccf) |
| Open issues / PRs | 62 | [GitHub repo](https://github.com/mattsse/chromiumoxide) |
| Star | 1,438 | [GitHub repo](https://github.com/mattsse/chromiumoxide) |

维护状态：仍在维护，但节奏不快。0.9.1 到调研日已约 9 个月，仓库最后提交在 2026-04-03，62 个 open issue/PR。它是本次候选中下载量和生态最成熟的纯 Rust CDP 客户端。

#### API 层级

`chromiumoxide` 提供 `Browser`、`Page`、`Element` 等高层对象，但**没有 Locator 对象**，也没有 Playwright 的 `locator()` 链式 API。元素查找是立即执行的 `Page::find_element(selector)`，返回 `Element`（[src/page.rs:497-505](https://github.com/mattsse/chromiumoxide/blob/main/src/page.rs#L497-L505)）。元素动作是 `click()`、`type_str()`、`press_key()`、`scroll_into_view()`、`focus()`、`hover()` 等（[src/element.rs:210-323](https://github.com/mattsse/chromiumoxide/blob/main/src/element.rs#L210-L323)）。

**没有** `fill()` 或 `clear()`：`Element` 的文本输入入口只有 `type_str()` 和 `press_key()`（[src/element.rs:301-323](https://github.com/mattsse/chromiumoxide/blob/main/src/element.rs#L301-L323)）。也没有元素级的 `wait_for_element` / `wait_for_selector`；等待只有导航级别的 `Page::wait_for_navigation()`（[src/page.rs:351-357](https://github.com/mattsse/chromiumoxide/blob/main/src/page.rs#L351-L357)）。

#### click 语义

`Element::click()` 的源码只有三步（[src/element.rs:269-273](https://github.com/mattsse/chromiumoxide/blob/main/src/element.rs#L269-L273)）：

```rust
pub async fn click(&self) -> Result<&Self> {
    let center = self.scroll_into_view().await?.clickable_point().await?;
    self.tab.click(center).await?;
    Ok(self)
}
```

- **会滚动**：`scroll_into_view()` 先用 JS 检查 `this.isConnected`、`this.nodeType`，再用 `IntersectionObserver` 计算 `visibleRatio`；只有 `visibleRatio !== 1.0` 时才执行 `scrollIntoView({block:'center', inline:'center', behavior:'instant'})`（[src/element.rs:229-263](https://github.com/mattsse/chromiumoxide/blob/main/src/element.rs#L229-L263)）。也就是“完全可见才不滚；部分可见或不可见都会滚”，不是 Playwright 的“动作前按需滚动 + stable 检查”。
- **取点**：`clickable_point()` 调用 `DOM.getContentQuads`，过滤 8 个点的 quad、面积大于 1 的 quad，取第一个 quad 的中心；没有 quad 时报 “Node is either not visible or not an HTMLElement”（[src/element.rs:142-160](https://github.com/mattsse/chromiumoxide/blob/main/src/element.rs#L142-L160)）。这里**没有** `elementFromPoint` 的 hit-target 检查，也没有 viewport 交集检查。
- **发事件**：`Page::click(point)` 在 `handler/page.rs` 里依次发 `mouseMoved`、`mousePressed`、`mouseReleased`，使用 `Input.dispatchMouseEvent`，是可信输入而不是 DOM `element.click()`（[src/handler/page.rs:193-227](https://github.com/mattsse/chromiumoxide/blob/main/src/handler/page.rs#L193-L227)）。
- **不等待 stable**：没有“两帧 bounding box 不变”的检查。
- **不等待 enabled/editable**：没有检查 `disabled` / `readonly`。
- **不重试**：`click()` 只调用一次；失败直接返回，没有 Playwright 的 `_retryAction` 循环。
- **不等待导航**：`click()` 本身不等待导航；需要调用方自己 `wait_for_navigation()`（[src/page.rs:351-357](https://github.com/mattsse/chromiumoxide/blob/main/src/page.rs#L351-L357)）。

因此，和 Playwright 相比，`chromiumoxide` 的 `click` 只覆盖了“滚动 + 中心点鼠标事件”，缺少 stable、enabled、receives events、retry 和 navigation wait。它比完全手写 CDP 强的地方是：协议类型、连接复用、启动/关闭、`scroll_into_view` 和中心点计算已经封装好了。

#### fill 语义

没有 `fill()`。`type_str()` 的文档只写 “Type the input”（[src/element.rs:301-304](https://github.com/mattsse/chromiumoxide/blob/main/src/element.rs#L301-L304)），实现是按字符逐个发 `Input.dispatchKeyEvent` 的 `keyDown` / `keyUp`（[src/handler/page.rs:237-242](https://github.com/mattsse/chromiumoxide/blob/main/src/handler/page.rs#L237-L242)）。它**不会先清空输入框**，也不会检查 `editable`。如果输入框已有值，需要调用方自己全选/删除，或者用 JS 设置 `value`。这与 Playwright `fill()` 的“聚焦 + 设置值 + 触发 input 事件 + 清空”不一致。

#### 浏览器支持

- **Edge**：`DetectionOptions` 默认 `msedge: true`，会查找 `msedge`、`microsoft-edge`、`microsoft-edge-stable` 等命令名（[src/detection.rs:4-16](https://github.com/mattsse/chromiumoxide/blob/main/src/detection.rs#L4-L16)、[src/detection.rs:71-75](https://github.com/mattsse/chromiumoxide/blob/main/src/detection.rs#L71-L75)）；Windows 还会查 `C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`（[src/detection.rs:99-101](https://github.com/mattsse/chromiumoxide/blob/main/src/detection.rs#L99-L101)）。也可以显式 `chrome_executable(path)` 指向 Edge（[src/browser/config.rs:238-241](https://github.com/mattsse/chromiumoxide/blob/main/src/browser/config.rs#L238-L241)）。
- **持久 profile**：`BrowserConfigBuilder::user_data_dir(path)`（[src/browser/config.rs:233-236](https://github.com/mattsse/chromiumoxide/blob/main/src/browser/config.rs#L233-L236)），启动时拼 `--user-data-dir=<path>`（[src/browser/config.rs:397-406](https://github.com/mattsse/chromiumoxide/blob/main/src/browser/config.rs#L397-L406)）。
- **headless**：默认 headless（`HeadlessMode::True`，[src/browser/config.rs:143](https://github.com/mattsse/chromiumoxide/blob/main/src/browser/config.rs#L143)）；`with_head()` 切 headful，`new_headless_mode()` 切 `--headless=new`（[src/browser/config.rs:176-186](https://github.com/mattsse/chromiumoxide/blob/main/src/browser/config.rs#L176-L186)）。启动参数按模式拼 `--headless` 或 `--headless=new`（[src/browser/config.rs:414-430](https://github.com/mattsse/chromiumoxide/blob/main/src/browser/config.rs#L414-L430)）。
- **启动方式**：`BrowserConfig::launch()` 用 `--remote-debugging-port=0` 和 `--user-data-dir` 拉起浏览器（[src/browser/config.rs:377-406](https://github.com/mattsse/chromiumoxide/blob/main/src/browser/config.rs#L377-L406)），`Browser::launch` 从子进程 stderr 解析 WebSocket URL 并连接（[src/browser/mod.rs:154-180](https://github.com/mattsse/chromiumoxide/blob/main/src/browser/mod.rs#L154-L180)）。README 的 Troubleshooting 也说明它“tries to parse the debugging port from the chromium process output”（[README:117-121](https://github.com/mattsse/chromiumoxide/blob/main/README.md#L117-L121)）。

#### 运行时模型与平台

- **async / tokio**：README 明确写 “`chromiumoxide` only supports the `tokio` runtime.”（[README:70](https://github.com/mattsse/chromiumoxide/blob/main/README.md#L70)）。`Cargo.toml` 依赖 `tokio`、`async-tungstenite`、`serde`（[Cargo.toml:17-30](https://github.com/mattsse/chromiumoxide/blob/main/Cargo.toml#L17-L30)）。
- **Windows/Linux**：检测逻辑同时覆盖 Windows 注册表/Edge 路径、Linux 常见命令名与 macOS 路径（[src/detection.rs](https://github.com/mattsse/chromiumoxide/blob/main/src/detection.rs#L24-L140)）。Linux 以 root 运行时需要 `no_sandbox()`（[src/browser/config.rs:171-173](https://github.com/mattsse/chromiumoxide/blob/main/src/browser/config.rs#L171-L173)、[src/browser/config.rs:409-411](https://github.com/mattsse/chromiumoxide/blob/main/src/browser/config.rs#L409-L411)）。
- **平台 caveat**：没有针对 Edge 的专门测试；Edge 通过 Chromium 系二进制路径工作。headful 模式需要桌面会话；云端的 headless 模式默认可用。

#### 与 Playwright 对比

| 能力 | Playwright | `chromiumoxide` |
|---|---|---|
| Locator 对象 | 有 | 无，只有 `find_element(selector)` |
| 动作前滚动 | 有，按需 | 有，`scroll_into_view()` |
| visible | 有，非空 bounding box + 非 `visibility:hidden` | 部分，content quads + `IntersectionObserver`，不是同一个定义 |
| stable | 有，两帧 bounding box 不变 | 无 |
| receives events / hit target | 有 | 无 |
| enabled / editable | 有 | 无 |
| 失败重试 | 有，直到 timeout | 无，单次调用 |
| 导航等待 | 有，可配置 | 无，需要显式 `wait_for_navigation()` |
| `fill` | 有，聚焦 + 设值 + input 事件 | 无，只有逐字符 `type_str()` |
| 元素等待 | `locator.wait_for()` | 无，只有导航等待 |

结论：`chromiumoxide` 是“纯 Rust CDP 客户端里最稳的底座”，但不是“Playwright-like 高层 API”。如果选它，仓库仍需自己实现 actionability + 重试 + `fill` 语义。

---

### 3.2 `headless_chrome` 1.0.22

#### 版本、维护、许可证

| 项目 | 值 | 依据 |
|---|---|---|
| 最新版本 / 发布日期 | 1.0.22 / 2026-06-11 | [crates.io API](https://crates.io/api/v1/crates/headless_chrome) |
| 下载量（总 / 近期） | 3,636,615 / 684,077 | [crates.io API](https://crates.io/api/v1/crates/headless_chrome) |
| 许可证 | MIT | [Cargo.toml:8](https://github.com/rust-headless-chrome/rust-headless-chrome/blob/main/Cargo.toml#L8) |
| MSRV | 1.85 | [Cargo.toml:6](https://github.com/rust-headless-chrome/rust-headless-chrome/blob/main/Cargo.toml#L6) |
| Cargo features | `default = ["offline"]`；`fetch` 启用下载 Chromium 快照 | [Cargo.toml:53-55](https://github.com/rust-headless-chrome/rust-headless-chrome/blob/main/Cargo.toml#L53-L55) |
| 最后提交 | `0a5c307`，2026-06-11 | [commit](https://github.com/rust-headless-chrome/rust-headless-chrome/commit/0a5c307a85debc450378a1f19d4eac1838d7b22d) |
| Open issues / PRs | 144 | [GitHub repo](https://github.com/rust-headless-chrome/rust-headless-chrome) |
| Star | 2,960 | [GitHub repo](https://github.com/rust-headless-chrome/rust-headless-chrome) |

维护状态：仍在发布（1.0.20 → 1.0.22），最后提交与最新发布同日；但 144 个 open issue/PR，说明积压较多。它是 Puppeteer 的 Rust 等价物（[README:8-10](https://github.com/rust-headless-chrome/rust-headless-chrome/blob/main/README.md#L8-L10)），API 是同步的。

#### API 层级

`Tab` 和 `Element` 提供 `find_element`、`find_elements`、`wait_for_element`、`wait_until_visible`、`click`、`type_into`、`press_key`、`scroll_into_view`、`call_js_fn` 等（[src/browser/tab/mod.rs:623-672](https://github.com/rust-headless-chrome/rust-headless-chrome/blob/main/src/browser/tab/mod.rs#L623-L672)、[src/browser/tab/element/mod.rs:117-282](https://github.com/rust-headless-chrome/rust-headless-chrome/blob/main/src/browser/tab/element/mod.rs#L117-L282)）。

**没有 Locator 对象**，也没有 `fill()` / `clear()`。`wait_for_element` 等的是元素“出现”，不是“可操作”：源码用 `util::Wait::with_timeout(...).strict_until(|| self.find_element(selector), ...)`（[src/browser/tab/mod.rs:631-641](https://github.com/rust-headless-chrome/rust-headless-chrome/blob/main/src/browser/tab/mod.rs#L631-L641)）。`wait_until_visible` 才检查 `this.checkVisibility()`（[src/browser/tab/element/mod.rs:590-610](https://github.com/rust-headless-chrome/rust-headless-chrome/blob/main/src/browser/tab/element/mod.rs#L590-L610)）。

#### click 语义

`Element::click()` 源码（[src/browser/tab/element/mod.rs:266-272](https://github.com/rust-headless-chrome/rust-headless-chrome/blob/main/src/browser/tab/element/mod.rs#L266-L272)）：

```rust
pub fn click(&self) -> Result<&Self> {
    self.scroll_into_view()?;
    let midpoint = self.get_midpoint()?;
    self.parent.click_point(midpoint)?;
    Ok(self)
}
```

- **会滚动**：`scroll_into_view()` 的 JS 与 `chromiumoxide` 几乎相同：`isConnected` / `nodeType` 检查、`IntersectionObserver` 的 `visibleRatio`，只有 `visibleRatio !== 1.0` 才 `scrollIntoView({block:'center', inline:'center', behavior:'instant'})`（[src/browser/tab/element/mod.rs:450-484](https://github.com/rust-headless-chrome/rust-headless-chrome/blob/main/src/browser/tab/element/mod.rs#L450-L484)）。
- **取点**：`get_midpoint()` 先用 `DOM.getContentQuads`，取第一个 quad 的 `(bottom_right + top_left) / 2`；失败时退回 JS：最多等 20 秒，等 `getBoundingClientRect().x != 0`，再 `scrollIntoView()` 并重新取 rect（[src/browser/tab/element/mod.rs:523-579](https://github.com/rust-headless-chrome/rust-headless-chrome/blob/main/src/browser/tab/element/mod.rs#L523-L579)）。这仍然**不是** Playwright 的 stable 检查，也没有 hit-target 检查。
- **发事件**：`Tab::click_point()` 依次 `move_mouse_to_point`、`optional_slow_motion_sleep(250)`、`Input.dispatchMouseEvent(MousePressed)`、`Input.dispatchMouseEvent(MouseReleased)`（[src/browser/tab/mod.rs:1024-1069](https://github.com/rust-headless-chrome/rust-headless-chrome/blob/main/src/browser/tab/mod.rs#L1024-L1069)）。
- **不等待 enabled/editable/stable**：`click()` 不调用 `wait_until_visible()`；即使调用，`checkVisibility()` 也只检查 CSS 可见性，不等价于 Playwright 的 visible（Playwright 还要求非空 bounding box）。
- **不重试**：`click()` 是单次调用，没有重试循环。
- **不等待导航**：`click_point()` 本身不等待导航。

#### fill 语义

`Element::type_into(text)` 先 `click()`，再 `parent.type_str(text)`（[src/browser/tab/element/mod.rs:274-282](https://github.com/rust-headless-chrome/rust-headless-chrome/blob/main/src/browser/tab/element/mod.rs#L274-L282)）。`Tab::type_str()` 对每个字符查键定义并发 `keyDown`/`keyUp`，没有键定义时用 `Input.insertText`（[src/browser/tab/mod.rs:875-916](https://github.com/rust-headless-chrome/rust-headless-chrome/blob/main/src/browser/tab/mod.rs#L875-L916)）。它**不会清空已有值**，也没有 `editable` 检查。

#### 浏览器支持

- **Edge**：`default_executable()` 的候选命令名包含 `microsoft-edge-stable`、`microsoft-edge-beta`、`microsoft-edge-dev`、`msedge`、`microsoft-edge`（[src/browser/mod.rs:550-556](https://github.com/rust-headless-chrome/rust-headless-chrome/blob/main/src/browser/mod.rs#L550-L556)）；Windows 还会查 `C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`（[src/browser/mod.rs:593](https://github.com/rust-headless-chrome/rust-headless-chrome/blob/main/src/browser/mod.rs#L593)）。
- **持久 profile**：`LaunchOptions.user_data_dir`（[src/browser/process.rs:127-131](https://github.com/rust-headless-chrome/rust-headless-chrome/blob/main/src/browser/process.rs#L127-L131)）；不指定时每次用临时目录（[src/browser/process.rs:355-369](https://github.com/rust-headless-chrome/rust-headless-chrome/blob/main/src/browser/process.rs#L355-L369)）。
- **headless**：`LaunchOptions.headless` 默认 `true`（[src/browser/process.rs:86-88](https://github.com/rust-headless-chrome/rust-headless-chrome/blob/main/src/browser/process.rs#L86-L88)），启动时只拼 `--headless`（[src/browser/process.rs:414-415](https://github.com/rust-headless-chrome/rust-headless-chrome/blob/main/src/browser/process.rs#L414-L415)）。**没有 `--headless=new` 选项**。
- **启动方式**：选一个可用端口，拼 `--remote-debugging-port=<port>` 和 `--user-data-dir`（[src/browser/process.rs:345-369](https://github.com/rust-headless-chrome/rust-headless-chrome/blob/main/src/browser/process.rs#L345-L369)），从子进程 stderr 解析 WebSocket URL（[src/browser/process.rs:300-320](https://github.com/rust-headless-chrome/rust-headless-chrome/blob/main/src/browser/process.rs#L300-L320)）。
- **可选 `fetch` feature** 会从 `chromium-browser-snapshots` 下载 Chromium 二进制，而不是 Edge（[README:87-91](https://github.com/rust-headless-chrome/rust-headless-chrome/blob/main/README.md#L87-L91)、[src/browser/fetcher.rs:358-389](https://github.com/rust-headless-chrome/rust-headless-chrome/blob/main/src/browser/fetcher.rs#L358-L389)）。

#### 运行时模型与平台

- **同步、std 线程**：README 明确说它 “has a synchronous API and is just implemented using plain old threads”，并以此和 fantoccini 对比（[README:120](https://github.com/rust-headless-chrome/rust-headless-chrome/blob/main/README.md#L120)）。没有 tokio 依赖。
- **Windows/Linux**：Windows 有注册表检测和 Edge 路径；Linux 有常见命令名和路径（[src/browser/mod.rs:536-600](https://github.com/rust-headless-chrome/rust-headless-chrome/blob/main/src/browser/mod.rs#L536-L600)）。
- **平台 caveat**：旧 headless；`enable_logging` 在 Windows 10+ 可能产生多余 shell 窗口（README 指向 [issue #371](https://github.com/rust-headless-chrome/rust-headless-chrome/issues/371)）；Linux 可能需要 sandbox 配置（[README:134](https://github.com/rust-headless-chrome/rust-headless-chrome/blob/main/README.md#L134)）。

#### 与 Playwright 对比

和 `chromiumoxide` 基本同档：有滚动和可信鼠标事件，没有 Locator、stable、enabled/editable、hit-target、重试、`fill`。额外优势是同步 API，接入现有同步代码更直接；劣势是只有旧 headless，且 144 个 open issue/PR 积压。

---

### 3.3 `ferridriver` 0.7.0

#### 版本、维护、许可证

| 项目 | 值 | 依据 |
|---|---|---|
| 最新版本 / 发布日期 | 0.7.0 / 2026-11-15 | [crates.io API](https://crates.io/api/v1/crates/ferridriver) |
| 下载量（总 / 近期） | 992 / 474 | [crates.io API](https://crates.io/api/v1/crates/ferridriver) |
| 许可证 | MIT OR Apache-2.0 | [workspace Cargo.toml:30](https://github.com/salamaashoush/ferridriver/blob/main/Cargo.toml#L30)、[README:9](https://github.com/salamaashoush/ferridriver/blob/main/README.md#L9) |
| MSRV | README 写 1.91；workspace `rust-version` 写 1.97.0 | [README:8](https://github.com/salamaashoush/ferridriver/blob/main/README.md#L8)、[Cargo.toml:29-30](https://github.com/salamaashoush/ferridriver/blob/main/Cargo.toml#L29-L30) |
| Cargo features | 无 `[features]`；后端由 `BrowserType` 选择 | [README:11-14](https://github.com/salamaashoush/ferridriver/blob/main/README.md#L11-L14) |
| 最后提交 | `efbfb9c`，2026-11-15 | [commit](https://github.com/salamaashoush/ferridriver/commit/efbfb9c638187dbef77a06340c1d70038a128260) |
| Open issues / PRs | 0 | [GitHub repo](https://github.com/salamaashoush/ferridriver) |
| Star | 6 | [GitHub repo](https://github.com/salamaashoush/ferridriver) |

维护状态：开发非常活跃（最后提交与最新发布同一天），但项目自己声明 “pre-1.0. The API tracks Playwright closely but is not API-stable — expect breaking changes between minor versions.”（[README:26-27](https://github.com/salamaashoush/ferridriver/blob/main/README.md#L26-L27)）。只有 6 个 star、约 992 次下载，不能把“活跃”误读成“被广泛验证”。

#### API 层级

它有真正的 `Locator`：`Frame::locator(selector)` 返回 `Locator`（[crates/ferridriver/src/frame.rs:490-509](https://github.com/salamaashoush/ferridriver/blob/main/crates/ferridriver/src/frame.rs#L490-L509)）；`Locator::click()`、`fill()`、`wait_for()` 都是公开动作（[crates/ferridriver/src/locator.rs:706-724](https://github.com/salamaashoush/ferridriver/blob/main/crates/ferridriver/src/locator.rs#L706-L724)、[crates/ferridriver/src/locator.rs:784-810](https://github.com/salamaashoush/ferridriver/blob/main/crates/ferridriver/src/locator.rs#L784-L810)、[crates/ferridriver/src/locator.rs:1851-1856](https://github.com/salamaashoush/ferridriver/blob/main/crates/ferridriver/src/locator.rs#L1851-L1856)）。定位器支持 strict mode，`retry_resolve!` 会在 strict violation 时返回类型化错误，其余情况在 deadline 前重新解析（[crates/ferridriver/src/locator.rs:180-250](https://github.com/salamaashoush/ferridriver/blob/main/crates/ferridriver/src/locator.rs#L180-L250)）。

#### click 语义

`click_prep()` 的注释直接写明它对齐 Playwright 的 `_performPointerAction`（[crates/ferridriver/src/actions.rs:275-300](https://github.com/salamaashoush/ferridriver/blob/main/crates/ferridriver/src/actions.rs#L275-L300)）：

> “Pointer-action actionability (Playwright `_performPointerAction`): visible + enabled + stable. `clickPrep` is async (the `stable` gate does rAF box-stability sampling page-side); `call_js_fn_value` awaits the returned promise, so this stays ONE round-trip.”

`click_with_opts()` 的注释写明它对齐 Playwright 的 `_retryAction`（[crates/ferridriver/src/actions.rs:946-1020](https://github.com/salamaashoush/ferridriver/blob/main/crates/ferridriver/src/actions.rs#L946-L1020)）：

> “Retry the entire pointer action when the hit-target interceptor reports another element captured the click — mirrors Playwright's `_retryAction` loop …”
>
> “Cycle scroll alignment across retries — Playwright `_retryPointerAction` (`undefined` => precise `scrollIntoViewIfNeeded`, then end/center/start) to reveal targets under `position:sticky` overlays.”

它的重试退避是 `[0, 20, 100, 100, 500]` 毫秒，并在每次重试时循环 `scroll_align`：`None` → `{block:'end',inline:'end'}` → `{block:'center',inline:'center'}` → `{block:'start',inline:'start'}`（[crates/ferridriver/src/actions.rs:956-1005](https://github.com/salamaashoush/ferridriver/blob/main/crates/ferridriver/src/actions.rs#L956-L1005)）。这已经覆盖了 Playwright 的滚动、hit-target、stable、重试四个关键点。`Locator::click_impl()` 还在点击后做 bounded navigation settle（[crates/ferridriver/src/locator.rs:706-724](https://github.com/salamaashoush/ferridriver/blob/main/crates/ferridriver/src/locator.rs#L706-L724)）。

#### fill 语义

`fill()` 的注释写 “`actions::fill(..., force)` runs `checkElementStates(['visible', 'enabled','editable'])` internally when `force` is false”，并且用元素自己的 setter 写入，让 React 这类受控输入能观察到值变化（[crates/ferridriver/src/actions.rs:408-455](https://github.com/salamaashoush/ferridriver/blob/main/crates/ferridriver/src/actions.rs#L408-L455)）。`fill_impl` 通过 `retry_resolve!` 在 timeout 内重试（[crates/ferridriver/src/locator.rs:784-810](https://github.com/salamaashoush/ferridriver/blob/main/crates/ferridriver/src/locator.rs#L784-L810)）。这比 `chromiumoxide` / `headless_chrome` 的逐字符 `type_str` 更接近 Playwright `fill()`。

#### 浏览器支持

- **Chromium CDP**：README 写明四个后端，其中 Chromium 走 CDP pipe 或 CDP WebSocket（[README:11-14](https://github.com/salamaashoush/ferridriver/blob/main/README.md#L11-L14)）。
- **Edge**：`LaunchOptions.executable_path` 可指向任意浏览器二进制（[crates/ferridriver/src/options.rs:2208-2212](https://github.com/salamaashoush/ferridriver/blob/main/crates/ferridriver/src/options.rs#L2208-L2212)）；但 `channel` 的注释说它目前是 “surface-only — the bundled-browser resolver reads this when selecting between the headless shell and a channel-specific Chrome install”，并举例 `"msedge"`（[crates/ferridriver/src/options.rs:2217-2221](https://github.com/salamaashoush/ferridriver/blob/main/crates/ferridriver/src/options.rs#L2217-L2221)）。因此 Edge 最可靠的方式是显式 `executable_path`，`channel: "msedge"` 不能当作已实现。
- **持久 profile**：`launch_persistent_context(user_data_dir, options)`（[crates/ferridriver/src/browser_type.rs:365-371](https://github.com/salamaashoush/ferridriver/blob/main/crates/ferridriver/src/browser_type.rs#L365-L371)），内部设置 `plan.user_data_dir` 并标记 `persistent_context = true`（[crates/ferridriver/src/browser_type.rs:385-393](https://github.com/salamaashoush/ferridriver/blob/main/crates/ferridriver/src/browser_type.rs#L385-L393)）。
- **headless**：`LaunchOptions.headless: Option<bool>`，默认 `true`（[crates/ferridriver/src/options.rs:2208-2209](https://github.com/salamaashoush/ferridriver/blob/main/crates/ferridriver/src/options.rs#L2208-L2209)、[crates/ferridriver/src/options.rs:2396](https://github.com/salamaashoush/ferridriver/blob/main/crates/ferridriver/src/options.rs#L2396)）；提供 headless shell 的安装路径（[crates/ferridriver/src/install.rs:803-805](https://github.com/salamaashoush/ferridriver/blob/main/crates/ferridriver/src/install.rs#L803-L805)）。

#### 运行时模型与平台

- **async / tokio**：核心依赖 `tokio`、`tokio-tungstenite`（[crates/ferridriver/Cargo.toml:18-30](https://github.com/salamaashoush/ferridriver/blob/main/crates/ferridriver/Cargo.toml#L18-L30)）。
- **Windows/Linux**：源码有大量 `#[cfg(windows)]` 分支（例如 [backend/process.rs:139](https://github.com/salamaashoush/ferridriver/blob/main/crates/ferridriver/src/backend/process.rs#L139)），说明 Windows 进程启动路径存在；但 README 的安装脚本只列 Linux/macOS（[README:57-65](https://github.com/salamaashoush/ferridriver/blob/main/README.md#L57-L65)），Windows 未被文档化验证。Edge 也未在文档中声明。
- **无 Node driver**：README 明确 “**No Node or Bun is involved in the run path.**”（[README:24](https://github.com/salamaashoush/ferridriver/blob/main/README.md#L24)）。WebKit 后端复用 Playwright 的 WebKit 二进制，但 Chromium CDP 路径是纯 Rust。

#### 与 Playwright 对比

这是本次调研中语义最接近的纯 Rust 实现：Locator、visible/enabled/stable、hit-target、滚动对齐、重试、`fill` 的 editable 检查都有。差距在成熟度：pre-1.0、API 不稳定、6 star、约 992 次下载、Windows/Edge 未验证。若选它，必须 pin 精确版本、把 `LoginPage` 适配层写薄、并在 Windows headful Edge + 持久 profile 上做真实回归。

---

### 3.4 `rustwright`（原生 Rust API）0.3.0

#### 版本、维护、许可证

| 项目 | 值 | 依据 |
|---|---|---|
| 最新版本 / 发布日期 | 0.3.0 / 2026-09-30 | [crates.io API](https://crates.io/api/v1/crates/rustwright) |
| 下载量（总 / 近期） | 809 / 773 | [crates.io API](https://crates.io/api/v1/crates/rustwright) |
| 许可证 | MIT | [Cargo.toml:7](https://github.com/Skyvern-AI/rustwright/blob/main/Cargo.toml#L7) |
| MSRV | 1.85 | [Cargo.toml:5](https://github.com/Skyvern-AI/rustwright/blob/main/Cargo.toml#L5) |
| Cargo features | 默认 `python`（PyO3 绑定）；`test-support` 仅测试；Rust 原生 crate 直接依赖 core | [Cargo.toml:21-32](https://github.com/Skyvern-AI/rustwright/blob/main/Cargo.toml#L21-L32) |
| 最后提交 | `16aabd1`，2026-11-03（仓库 pushed 2026-11-17） | [commit](https://github.com/Skyvern-AI/rustwright/commit/16aabd1660c264fc0a18e3dfbfd6888e3463c8bc) |
| Open issues / PRs | 27 | [GitHub repo](https://github.com/Skyvern-AI/rustwright) |
| Star | 960（整个 Rustwright 项目） | [GitHub repo](https://github.com/Skyvern-AI/rustwright) |

维护状态：整个 Rustwright 项目活跃，但 Rust 原生 API 明确标为 Alpha、Chromium-only：“Alpha; Chromium-only.”（[rust-native/README:22](https://github.com/Skyvern-AI/rustwright/blob/main/rust-native/README.md#L22)）。它的主要交付物是 Python/Node 绑定；Rust 原生 crate 是“薄封装”（[rust-native/README:3-8](https://github.com/Skyvern-AI/rustwright/blob/main/rust-native/README.md#L3-L8)）。

#### API 层级

`rustwright` crate 提供同步的 `click(selector, options)`、`fill(selector, value, options)`、`dblclick`、`drag_and_drop`、`press_key`、`scroll_into_view` 等（[rust-native/src/lib.rs:979-990](https://github.com/Skyvern-AI/rustwright/blob/main/rust-native/src/lib.rs#L979-L990)、[rust-native/src/lib.rs:1073-1080](https://github.com/Skyvern-AI/rustwright/blob/main/rust-native/src/lib.rs#L1073-L1080)）。它是**选择器 API，不是 Locator API**：没有 `page.locator()` 链式对象，也没有 `nth` / `filter` / `get_by_role` 这一层。

#### click 语义

`click` 的文档写得很直接（[rust-native/src/lib.rs:979-981](https://github.com/Skyvern-AI/rustwright/blob/main/rust-native/src/lib.rs#L979-L981)）：

> “Strictly resolve `selector`, auto-wait for actionability, and click it through Chromium's physical mouse input pipeline.”

核心里的 actionability 失败消息覆盖了 Playwright 的关键检查（[src/lib.rs:2100-2104](https://github.com/Skyvern-AI/rustwright/blob/main/src/lib.rs#L2100-L2104)）：

> “actionability check failed: element is not visible”
>
> “actionability check failed: element did not become stable”
>
> “actionability check failed: element is not receiving pointer events”
>
> “actionability check failed: element is disabled”
>
> “actionability check failed: element was detached while waiting for actionability”

这些消息被标记为 `FailureKind::Actionability`、`retryable: Some(true)`，并在 timeout 前重试（[src/lib.rs:2090-2110](https://github.com/Skyvern-AI/rustwright/blob/main/src/lib.rs#L2090-L2110)、[src/lib.rs:2600-2611](https://github.com/Skyvern-AI/rustwright/blob/main/src/lib.rs#L2600-L2611)）。这比 `chromiumoxide` / `headless_chrome` 强很多，已经覆盖 visible、stable、receives pointer events、disabled、detached。

#### fill 语义

`fill(selector, value, options)` 是公开 API（[rust-native/src/lib.rs:1073-1080](https://github.com/Skyvern-AI/rustwright/blob/main/rust-native/src/lib.rs#L1073-L1080)）；核心有 `fill` 的 guard/actionability 路径（[src/lib.rs:36500-36561](https://github.com/Skyvern-AI/rustwright/blob/main/src/lib.rs#L36500-L36561)）。它同样没有 Locator 链，且文档没有像 Playwright 那样逐条列出 `fill` 的 focus/input/清空行为，需要读实现确认。

#### 浏览器支持

- **Chromium-only**：README 明确 “Alpha; Chromium-only.”（[README:5](https://github.com/Skyvern-AI/rustwright/blob/main/README.md#L5)）；Firefox/WebKit “are **not planned**”（[README:344](https://github.com/Skyvern-AI/rustwright/blob/main/README.md#L344)）。
- **Edge**：`LaunchOptions.executable_path` 可以指向任意 Chromium 系二进制（[rust-native/src/lib.rs:130-171](https://github.com/Skyvern-AI/rustwright/blob/main/rust-native/src/lib.rs#L130-L171)），但项目只声明 Chromium，Edge 未测试。
- **持久 profile**：`LaunchOptions.user_data_dir`（[rust-native/src/lib.rs:138](https://github.com/Skyvern-AI/rustwright/blob/main/rust-native/src/lib.rs#L138)）。
- **headless**：`LaunchOptions.headless`（[rust-native/src/lib.rs:130](https://github.com/Skyvern-AI/rustwright/blob/main/rust-native/src/lib.rs#L130)）。

#### 运行时模型与平台

- **同步原生 facade + 异步 core**：README 示例是 `fn main()` 同步调用 `chromium().launch(...)`、`page.goto(...)`（[rust-native/README:7-20](https://github.com/Skyvern-AI/rustwright/blob/main/rust-native/README.md#L7-L20)）；底层是 Tokio + CDP 的异步引擎（[README:122](https://github.com/Skyvern-AI/rustwright/blob/main/README.md#L122)）。
- **Windows/Linux**：README 未给出 Rust 原生 API 的平台矩阵；Python/Node 绑定覆盖 Windows/Linux，但 Rust 原生 crate 未声明。**未验证**。

#### 与 Playwright 对比

actionability 语义接近 Playwright，滚动和重试由核心处理；但没有 Locator，selector 严格解析只是 Locator 的一部分替代。Alpha + Chromium-only，不能作为当前 Windows Edge 登录驱动的唯一依赖。

---

### 3.5 `playwright-cdp` 0.3.0

#### 版本、维护、许可证

| 项目 | 值 | 依据 |
|---|---|---|
| 最新版本 / 发布日期 | 0.3.0 / 2026-07-10 | [crates.io API](https://crates.io/api/v1/crates/playwright-cdp) |
| 下载量（总 / 近期） | 116 / 39 | [crates.io API](https://crates.io/api/v1/crates/playwright-cdp) |
| 许可证 | Apache-2.0 | [Cargo.toml:7](https://github.com/liangdi/playwright-cdp/blob/master/Cargo.toml#L7) |
| MSRV | 1.85 | [Cargo.toml:5](https://github.com/liangdi/playwright-cdp/blob/master/Cargo.toml#L5) |
| Cargo features | `screenshot-diff`（可选，PNG 像素比较） | [Cargo.toml:21-26](https://github.com/liangdi/playwright-cdp/blob/master/Cargo.toml#L21-L26) |
| 最后提交 | `3541acd`，2026-07-10 | [commit](https://github.com/liangdi/playwright-cdp/commit/3541acd9ca2948fc8ea23adfee606499ae1e6bb3) |
| Open issues / PRs | 0 | [GitHub repo](https://github.com/liangdi/playwright-cdp) |
| Star | 2 | [GitHub repo](https://github.com/liangdi/playwright-cdp) |

维护状态：有真实实现，但只有 2 个 star、116 次下载；README 还写着 “当前为早期 `0.1.x`”（[README:29](https://github.com/liangdi/playwright-cdp/blob/master/README.md#L29)），与 crate 的 0.3.0 不一致。不能作为生产依赖。

#### API 层级

它有 Playwright 形状的 `Locator`：`page.locator(selector)`、`locator.click()`、`locator.fill()`、`locator.wait_for()`（[src/locator.rs:187-200](https://github.com/liangdi/playwright-cdp/blob/master/src/locator.rs#L187-L200)、[src/locator.rs:494-517](https://github.com/liangdi/playwright-cdp/blob/master/src/locator.rs#L494-L517)）。

#### click 语义

`click_element()` 先 `DOM.scrollIntoViewIfNeeded`，再在非 `force` 时 `wait_actionable`，然后按 `position` 或中心点发 `mouseMoved` / `mousePressed` / `mouseReleased`（[src/locator.rs:1322-1355](https://github.com/liangdi/playwright-cdp/blob/master/src/locator.rs#L1322-L1355)）。

但 `wait_actionable()` 只检查 `visible`（[src/locator.rs:1286-1305](https://github.com/liangdi/playwright-cdp/blob/master/src/locator.rs#L1286-L1305)）：

```rust
let function = "(el) => self.__pwcdpInjected && self.__pwcdpInjected.elementState(el).visible";
```

**没有** stable、enabled、receives events/hit-target；`click_element()` 只等一次 visible，然后单次发事件，**没有重试**。`locator.wait_for()` 只检查 `count() > 0`，不检查 visible（[src/locator.rs:494-517](https://github.com/liangdi/playwright-cdp/blob/master/src/locator.rs#L494-L517)）。

#### fill 语义

`fill()` 直接 `el.focus(); el.value = text; dispatchEvent(input); dispatchEvent(change)`，没有 actionability 检查（[src/locator.rs:195-207](https://github.com/liangdi/playwright-cdp/blob/master/src/locator.rs#L195-L207)）。

#### 浏览器支持

- **Chromium-only**：README 明确 “仅支持 Chromium。Firefox/WebKit 返回的是一个 Chromium 后端的 `BrowserType` 并打印警告”（[README:166](https://github.com/liangdi/playwright-cdp/blob/master/README.md#L166)）。
- **Edge**：`LaunchOptions.executable_path` 可指向 Edge（[src/options.rs:13-36](https://github.com/liangdi/playwright-cdp/blob/master/src/options.rs#L13-L36)），但未声明 Edge 测试。
- **持久 profile**：`launch_persistent_context(user_data_dir, ...)`（[src/browser_type.rs:73-103](https://github.com/liangdi/playwright-cdp/blob/master/src/browser_type.rs#L73-L103)）。
- **headless**：默认 `--headless=new`（[src/browser_process.rs:64-66](https://github.com/liangdi/playwright-cdp/blob/master/src/browser_process.rs#L64-L66)）；用 `--remote-debugging-port=0` 并轮询 `DevToolsActivePort`（[src/browser_process.rs:215-245](https://github.com/liangdi/playwright-cdp/blob/master/src/browser_process.rs#L215-L245)）。

#### 运行时模型与平台

- **async / tokio**：`tokio` + `tokio-tungstenite`（[Cargo.toml:29-41](https://github.com/liangdi/playwright-cdp/blob/master/Cargo.toml#L29-L41)）。
- **Windows/Linux**：源码没有平台限定；README 未声明平台矩阵。**未验证**。

#### 与 Playwright 对比

Locator 形状有了，但 actionability 只有 visible，没有 stable/enabled/hit-target/重试；`fill` 是直接赋值，不是 Playwright 的聚焦 + input 事件 + 清空语义。它更像“有 Locator 外观的轻量 CDP 客户端”，不是 Playwright 语义实现。

---

### 3.6 其他纯 Rust CDP 协议 crate

这些 crate 要么只提供协议类型/低层命令，要么是 `chromiumoxide` 之上的领域包装；都不提供完整的 Playwright Locator/actionability，因此不能直接替换 `LoginPage` 的高层动作。

| crate | 版本 / 日期 | 下载量（总 / 近期） | 许可证 | 维护 | API 层级 | 依据 |
|---|---|---|---|---|---|---|
| `chromiumoxide_cdp` | 0.9.1 / 2026-02-25 | 6,438,279 / 2,879,529 | MIT OR Apache-2.0 | 跟随 `chromiumoxide` | 生成的 CDP 命令/事件/类型，无 Locator | [crates.io API](https://crates.io/api/v1/crates/chromiumoxide_cdp)、[chromiumoxide README:Generated Code](https://github.com/mattsse/chromiumoxide/blob/main/README.md#generated-code) |
| `chromiumoxide_types` | 0.9.1 / 2026-02-25 | 6,339,645 / 2,880,818 | MIT OR Apache-2.0 | 跟随 `chromiumoxide` | 基础类型，无 Locator | [crates.io API](https://crates.io/api/v1/crates/chromiumoxide_types) |
| `cdp-driver` | 0.5.0 / 2026-11-07 | 100 / 32 | MIT | 新，活跃 | 677 commands / 232 events / 611 types 全部生成，原始协议 | [README](https://github.com/dreygur/cdp-protocol) |
| `cdp-lite` | 0.3.2 / 2026-11-08 | 2,694 / 1,560 | MIT | 新，活跃 | 轻量异步 CDP 客户端，原始协议 | [crates.io API](https://crates.io/api/v1/crates/cdp-lite) |
| `cdp-use-rs` | 0.1.1 / 2026-04-17 | 112 / 48 | MIT | 新 | type-safe CDP client，原始协议 | [README](https://github.com/netkn001/cdp-use-rs) |
| `cdp` | 0.0.0 / 2017-08-14 | 2,435 / 13 | MPL-2.0 | 占位 crate | “Reserved for https://github.com/devtools-html/cdp” | [crates.io API](https://crates.io/api/v1/crates/cdp) |
| `dravr-browser` | 1.0.0 / 2026-10-29 | 30,011 / 23,320 | MIT OR Apache-2.0 | 0 star，活跃 | 包装 `chromiumoxide` 的登录态/stealth 辅助函数；`click_element()` 是 DOM `el.click()`，不是 Playwright 的 Locator/actionability | [crates.io API](https://crates.io/api/v1/crates/dravr-browser)、[README](https://github.com/dravr-ai/dravr-browser)、[src/input.rs:224-249](https://github.com/dravr-ai/dravr-browser/blob/main/src/input.rs#L224-L249) |
| `stygian-browser` | 0.17.0 / 2026-08-24 | 2,541 / 472 | AGPL-3.0-only 或商业许可 | 反检测 CDP 库 | `click(page, x, y)` 是坐标级行为，不是 Playwright 高层 API | [crates.io API](https://crates.io/api/v1/crates/stygian-browser)、[src/behavior.rs:297](https://github.com/greysquirr3l/stygian/blob/main/crates/stygian-browser/src/behavior.rs#L297) |
| `browser-commander` | 0.22.1 / 2026-10-29 | 5,032 / 2,163 | Unlicense | 0 star | 包装 `chromiumoxide` / Playwright / Puppeteer / Selenium，不是纯 CDP | [crates.io API](https://crates.io/api/v1/crates/browser-commander)、[README](https://github.com/link-foundation/browser-commander) |

`chromiumoxide_cdp` 是 `chromiumoxide` 的生成代码 crate（[README:Generated Code](https://github.com/mattsse/chromiumoxide/blob/main/README.md#generated-code)），不是独立高层库。`cdp-driver`、`cdp-lite`、`cdp-use-rs` 都是“把协议命令暴露出来”的客户端，适合自己写动作层，但不提供 Locator/actionability。

---

## 4. 纯 Rust CDP 候选横向对比

| 维度 | `chromiumoxide` 0.9.1 | `headless_chrome` 1.0.22 | `ferridriver` 0.7.0 | `rustwright` 0.3.0 | `playwright-cdp` 0.3.0 |
|---|---|---|---|---|---|
| Locator 对象 | 无 | 无 | 有 | 无（选择器 API） | 有 |
| click 自动滚动 | 有（`scroll_into_view`） | 有（`scroll_into_view`） | 有（`clickPrep` + 滚动对齐循环） | 有 | 有（`DOM.scrollIntoViewIfNeeded`） |
| visible 检查 | 部分（content quads / IntersectionObserver） | 部分（`checkVisibility`，且 click 不调用） | 有 | 有 | 部分（只查 visible） |
| stable 检查 | 无 | 无 | 有 | 有 | 无 |
| receives events / hit target | 无 | 无 | 有 | 有 | 无 |
| enabled / editable | 无 | 无 | 有 | 有 | 无 |
| click 重试 | 无 | 无 | 有（退避 + 滚动对齐循环） | 有 | 无 |
| fill | 无（`type_str`） | 无（`type_into`） | 有（native setter + input 事件 + editable 检查） | 有 | 有（直接设 value，无 actionability） |
| 元素等待 | 无（只有导航等待） | 有（presence / visible，但 click 不用） | 有（`wait_for` 状态） | 无（只有 load state） | 有（只查 count） |
| Edge 启动 | 检测默认含 Edge；可显式 path | 检测含 Edge；可显式 path | `executable_path`；`channel: "msedge"` 仅 surface | `executable_path`；Chromium-only | `executable_path`；Chromium-only |
| 持久 profile | `user_data_dir` | `user_data_dir` | `launch_persistent_context` | `user_data_dir` | `launch_persistent_context` |
| headless | 旧 / 新 | 只有旧 | 有（含 headless shell） | 有 | `--headless=new` |
| 运行时 | async tokio | sync std 线程 | async tokio | sync facade / async core | async tokio |
| 成熟度 | 高（6.0M 下载，62 open） | 高（3.6M 下载，144 open） | 低（6 star，992 下载，pre-1.0） | Alpha（809 下载，Chromium-only） | 早期（116 下载，2 star） |

表中的具体行号与引用见 3.1–3.5 各小节；这里只做横向汇总。

---

## 5. 为什么不是 Playwright 绑定或 WebDriver 封装

这些库也能提供 Playwright 的 Locator/actionability 语义，但它们不是“纯 Rust 直接控制浏览器”：要么需要 Node.js + Playwright driver，要么需要 chromedriver/geckodriver/msedgedriver 进程。用户明确要求纯 Rust CDP 客户端，所以只列在这里说明排除理由。

| crate | 版本 / 日期 | 许可证 | 维护 | 运行时依赖 | 为什么不选 |
|---|---|---|---|---|---|
| `playwright`（`octaltree/playwright-rust`） | 0.0.20 / 2022-08-20 | MIT OR Apache-2.0（[Cargo.toml:6](https://github.com/octaltree/playwright-rust/blob/master/Cargo.toml#L6)） | 30 open issues/PRs，459 star，2022 后无提交 | Node.js + Playwright driver zip | 已停止维护；API 还没有 Locator（`src/api/api.json` 中 `Locator` 为 0 条） |
| `playwright-rs`（`padamson/playwright-rust`） | 0.21.0 / 2026-11-14 | Apache-2.0（[Cargo.toml:22](https://github.com/padamson/playwright-rust/blob/main/Cargo.toml#L22)） | 5 open issues/PRs，169 star，活跃 | Node.js + Playwright driver | 有完整 Locator/auto-wait，但仍是 JSON-RPC 到 Node server |
| `fantoccini` | 0.22.2 / 2026-11-15 | MIT OR Apache-2.0（[Cargo.toml:19](https://github.com/jonhoo/fantoccini/blob/main/Cargo.toml#L19)） | 31 open issues/PRs，2,035 star | 需要 WebDriver server（geckodriver/chromedriver/msedgedriver） | `Element::click()` 只是发 `ElementClick`；WebDriver 规范负责滚动/可交互检查，但没有 Playwright 的重试和 stable/hit-target 语义 |
| `thirtyfour` | 0.38.3 / 2026-11-14 | MIT OR Apache-2.0（[Cargo.toml:8](https://github.com/stevepryde/thirtyfour/blob/main/Cargo.toml#L8)） | 2 open issues/PRs，1,445 star | 需要 WebDriver server / chromedriver | `click_when_ready()` 只检查 displayed + enabled，文档明确说“not a complete interactability guarantee”，不重新解析、不重试 |
| `webdriver` | 0.54.0 / 2026-06-02 | MPL-2.0（[crates.io API](https://crates.io/api/v1/crates/webdriver)） | Mozilla 维护，5.3M 下载 | W3C WebDriver 协议库 | 它是协议/服务端库（`server`、`command` 模块），不是给应用直接驱动浏览器的客户端 |
| `cdp` | 0.0.0 / 2017-08-14 | MPL-2.0（[crates.io API](https://crates.io/api/v1/crates/cdp)） | 占位 | 无 | “Reserved for https://github.com/devtools-html/cdp”，没有可用实现 |

关键引用：

- `playwright` 的 README 写明 “built on top of Node.js library”，并有 “Playwright Driver” 一节：“All playwright client dependent on the driver: zip of core js library and Node.js.”（[README:9](https://github.com/octaltree/playwright-rust/blob/master/README.md#L9)、[README:50-52](https://github.com/octaltree/playwright-rust/blob/master/README.md#L50-L52)）。它的构建脚本从 `playwright.azureedge.net` 下载 driver zip（[src/build.rs:84](https://github.com/octaltree/playwright-rust/blob/master/src/build.rs#L84)）。
- `playwright-rs` 的 README 架构图写明 Rust API 通过 “JSON-RPC over stdio” 连接 “Playwright Server (Node.js/TypeScript)”（[README:110-123](https://github.com/padamson/playwright-rust/blob/main/README.md#L110-L123)）。
- `fantoccini` README：“These examples all assume that you have a WebDriver compatible process running on port 4444. A quick way to get one is to run `geckodriver` at the command line.”（[README:24-25](https://github.com/jonhoo/fantoccini/blob/main/README.md#L24-L25)）；`click()` 只发 `WebDriverCommand::ElementClick`（[src/elements.rs:458-467](https://github.com/jonhoo/fantoccini/blob/main/src/elements.rs#L458-L467)）。
- W3C WebDriver 规范对 Element Click 的定义：“The Element Click command scrolls into view the element if it is not already pointer-interactable, and clicks its in-view center point. If the element's center point is obscured by another element, an element click intercepted error is returned. If the element is outside the viewport, an element not interactable error is returned.”（[W3C WebDriver §14.1](https://www.w3.org/TR/webdriver1/#element-click)）。这提供了滚动和可交互检查，但没有 Playwright 的 stable 两帧检查、重试和超时语义。
- `thirtyfour` 的 `click_when_ready()` 文档：“This is equivalent to `self.wait_until().clickable().await?; self.click().await`. Here, clickable means displayed and enabled; it is not a complete interactability guarantee. This method acts on this already-resolved element, clicks once, and does not re-resolve a stale element, retry a failed click, or wait for an outcome.”（[thirtyfour/src/web_element.rs:328-350](https://github.com/stevepryde/thirtyfour/blob/main/thirtyfour/src/web_element.rs#L328-L350)）。
- `webdriver` 的 crates.io 描述是 “Library implementing the wire protocol for the W3C WebDriver specification.”（[crates.io API](https://crates.io/api/v1/crates/webdriver)），仓库指向 Mozilla 的 `testing/webdriver`（[repository](https://hg.mozilla.org/mozilla-central/file/tip/testing/webdriver)），docs.rs 的模块是 `actions`、`capabilities`、`command`、`common`、`error`、`httpapi`、`macros`、`response`、`server`（[docs.rs](https://docs.rs/webdriver/0.54.0/webdriver/)）。

---

## 6. 对本仓库的建议

现有 `LoginPage` trait 已经把浏览器动作收窄成 `click`/`fill`/`goto`/`wait` 等少数方法（[browser.rs:96-120](https://github.com/dacintliaoyuan/Chat2API-GatewayConsole/blob/main/rust/crates/gateway/src/login/browser.rs#L96-L120)），所以替换驱动只需要改 `SystemBrowserFactory` 返回的实现，不会影响登录状态机或 fake page 测试。基于这个前提：

### 方案 A（推荐，最低风险）：`chromiumoxide` + 仓库内 actionability 层

- 用 `BrowserConfig::builder().chrome_executable(edge_path).user_data_dir(profile).with_head()/headless_mode(...)` 启动 Edge（[config.rs:176-241](https://github.com/mattsse/chromiumoxide/blob/main/src/browser/config.rs#L176-L241)）。
- 在 `LoginPage::click` 里实现 Playwright 的五项检查：visible（非空 bounding box + 非 `visibility:hidden`）、stable（两帧同一 bounding box）、receives events（`elementFromPoint`）、enabled（`disabled`）、editable（`readonly`），失败后按 timeout 重试；滚动用 `scrollIntoView({block:'center'})`。
- `fill` 用 JS 聚焦 + 清空 + 设置值 + `input` 事件，或者 `click` 后全选删除再 `type_str`；不要直接用 `type_str` 当 `fill`。
- 代价：仍然要维护自己的 actionability 代码，但比现在手写 WebSocket + DevToolsActivePort + CDP 消息路由少很多；`chromiumoxide` 负责协议类型、连接、启动、关闭和滚动/点击的基础实现。
- 收益：6.0M 下载、1,438 star、Edge 检测默认开启、持久 profile 和 headless 都有，风险最低。

### 方案 B（语义最接近，但只建议做 spike）：`ferridriver`

- 直接使用 `Locator::click()` / `fill()` / `wait_for()`，由库处理 visible/enabled/stable、hit-target、滚动对齐和重试（[actions.rs:946-1020](https://github.com/salamaashoush/ferridriver/blob/main/crates/ferridriver/src/actions.rs#L946-L1020)）。
- 用 `executable_path` 指向 `msedge.exe`，用 `launch_persistent_context` 指定持久 profile。
- 先在一个分支上把 `LoginPage` 实现切到 `ferridriver`，跑真实 Edge headful + 持久 profile 的登录回归；如果通过，再考虑 pin 精确版本（例如 `=0.7.0`）并 vendor。
- 风险：pre-1.0、API 不稳定、6 star、Windows/Edge 未文档化验证；不适合直接作为生产唯一依赖。

### 方案 C（不新增依赖）：保留手写驱动，但补齐 Playwright 语义

- 现有 `click_once` 已有滚动、`elementFromPoint`、3 次重试和 2 秒预算（[browser.rs:922-990](https://github.com/dacintliaoyuan/Chat2API-GatewayConsole/blob/main/rust/crates/gateway/src/login/browser.rs#L922-L990)）。
- 缺的是 stable 两帧检查、enabled/editable、导航等待和统一的 `fill`。可以把 `ferridriver` 的 `click_prep`/`fill` 作为实现参考，但不引入依赖。
- 代价：继续维护自己的 CDP 子集，长期成本最高。

补充：如果目标只是“登录态 + 持久 profile + stealth”，`dravr-browser` 1.0.0 提供了 `chromiumoxide` 之上的领域封装（持久 profile、stealth、CDP 鼠标/键盘、网络捕获），下载量 30,011，MIT OR Apache-2.0（[crates.io API](https://crates.io/api/v1/crates/dravr-browser)、[README](https://github.com/dravr-ai/dravr-browser)）。但它的 `click_element()` 是 DOM `el.click()`（[src/input.rs:224-249](https://github.com/dravr-ai/dravr-browser/blob/main/src/input.rs#L224-L249)），`fill_input_field()` 是 click → 全选删除 → `Input.insertText`（[src/input.rs:121-134](https://github.com/dravr-ai/dravr-browser/blob/main/src/input.rs#L121-L134)），仍然没有 Playwright 的 Locator/actionability/重试。它可以作为启动与 profile 管理的参考，但不能解决 click 语义问题。

**建议**：生产路径选方案 A；同时用方案 B 做一个受控 spike，验证 `ferridriver` 在 Windows Edge、headful、持久 profile、Duo 页面上的真实表现。方案 C 只作为“不新增依赖”的兜底。

---

## 7. 未验证与限制

以下内容本次没有做真实运行验证，引用时需保留不确定性：

1. **没有实际启动 Edge/Chromium 做端到端对比**。本文的 click/fill 语义全部来自源码和官方文档，不是运行时行为测试；`chromiumoxide`、`headless_chrome`、`ferridriver`、`rustwright`、`playwright-cdp` 在 Windows headful Edge + 持久 profile 下的实际表现未验证。
2. **`ferridriver` 的 Windows/Edge 支持未文档化**。源码有 `#[cfg(windows)]` 分支（[backend/process.rs:139](https://github.com/salamaashoush/ferridriver/blob/main/crates/ferridriver/src/backend/process.rs#L139)），但 README 安装脚本只列 Linux/macOS（[README:57-65](https://github.com/salamaashoush/ferridriver/blob/main/README.md#L57-L65)）；`channel: "msedge"` 注释为 surface-only（[options.rs:2217-2221](https://github.com/salamaashoush/ferridriver/blob/main/crates/ferridriver/src/options.rs#L2217-L2221)）。
3. **`ferridriver` MSRV 不一致**。README 徽章写 1.91（[README:8](https://github.com/salamaashoush/ferridriver/blob/main/README.md#L8)），workspace `Cargo.toml` 写 `rust-version = "1.97.0"`（[Cargo.toml:29-30](https://github.com/salamaashoush/ferridriver/blob/main/Cargo.toml#L29-L30)）。实际构建以 workspace 为准。
4. **`rustwright` 的 Rust 原生 API 是 Alpha、Chromium-only**（[README:5](https://github.com/Skyvern-AI/rustwright/blob/main/README.md#L5)、[rust-native/README:22](https://github.com/Skyvern-AI/rustwright/blob/main/rust-native/README.md#L22)）；Edge 未声明，Windows/Linux 未声明。它的 actionability 实现来自 core，但原生 API 没有 Locator。
5. **`playwright-cdp` README 状态滞后**：README 写 “当前为早期 `0.1.x`”（[README:29](https://github.com/liangdi/playwright-cdp/blob/master/README.md#L29)），crate 实际是 0.3.0；它的 `wait_for` 只查 count，不查 visible，和注释“and, for actions, is visible”不一致（[src/locator.rs:494-517](https://github.com/liangdi/playwright-cdp/blob/master/src/locator.rs#L494-L517)）。
6. **GitHub open issue 数是 API 的 `open_issues_count`，包含 issue 与 PR**，不是纯 issue 数；数据是 2026-11-19 的快照。
7. **crates.io 下载量是累计值**，不代表生产采用度；`ferridriver` 的 992 次下载、`rustwright` 的 809 次下载与 `chromiumoxide` 的 6.0M 次下载不在一个量级。
8. **没有 benchmark**。本文没有测启动时间、内存、点击成功率或 Duo 页面通过率；这些需要真实环境回归。
9. **`headless_chrome` 的 `wait_until_visible` 使用 `checkVisibility()`**（[src/browser/tab/element/mod.rs:590-610](https://github.com/rust-headless-chrome/rust-headless-chrome/blob/main/src/browser/tab/element/mod.rs#L590-L610)），它不等价于 Playwright 的 visible 定义（Playwright 还要求非空 bounding box，[Actionability](https://playwright.dev/docs/actionability)）；且 `click()` 不调用它。
10. **`chromiumoxide` 的 `clickable_point()` 没有 viewport 交集/遮挡检查**（[src/element.rs:142-160](https://github.com/mattsse/chromiumoxide/blob/main/src/element.rs#L142-L160)），元素中心点在视口外或被他元素遮挡时可能点错目标；它没有 Playwright 的 hit-target 检查。

---

## 8. 参考来源

- Playwright Actionability：<https://playwright.dev/docs/actionability>
- Playwright `Locator.click`：<https://playwright.dev/docs/api/class-locator#locator-click>
- Playwright `Locator.fill`：<https://playwright.dev/docs/api/class-locator#locator-fill>
- Playwright `Locator.waitFor`：<https://playwright.dev/docs/api/class-locator#locator-wait-for>
- Playwright Locators：<https://playwright.dev/docs/locators>
- W3C WebDriver Element Click：<https://www.w3.org/TR/webdriver1/#element-click>
- W3C WebDriver Element Send Keys：<https://www.w3.org/TR/webdriver1/#element-send-keys>
- `chromiumoxide` 仓库：<https://github.com/mattsse/chromiumoxide>
- `headless_chrome` 仓库：<https://github.com/rust-headless-chrome/rust-headless-chrome>
- `ferridriver` 仓库：<https://github.com/salamaashoush/ferridriver>
- `rustwright` 仓库：<https://github.com/Skyvern-AI/rustwright>
- `playwright-cdp` 仓库：<https://github.com/liangdi/playwright-cdp>
- `playwright`（octaltree）仓库：<https://github.com/octaltree/playwright-rust>
- `playwright-rs`（padamson）仓库：<https://github.com/padamson/playwright-rust>
- `fantoccini` 仓库：<https://github.com/jonhoo/fantoccini>
- `thirtyfour` 仓库：<https://github.com/stevepryde/thirtyfour>
- `webdriver` crate：<https://crates.io/crates/webdriver>
- `cdp` crate：<https://crates.io/crates/cdp>
