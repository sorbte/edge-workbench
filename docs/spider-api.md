# 宠物联动接口文档（插件开发者指南）

「操作可视化」是工作台内置的一只宠物（当前上架：**蜘蛛**，后续在 `PETS` 注册表扩充），
在左侧菜单「宠物」页选择与管理。它以 8 条腿步态在网页可视区内活动，插件在
**要获取信息 / 完成动作的时刻**调用本文档的接口，宠物就会爬过去采集（高亮目标文字 +
粒子 + 涟漪），与自动化动作形成配合。采集是**指向性接触式**的：宠物径直冲刺到目标
位置（不做绕路巡游），**冲刺途中接触到的目标矩形内词条逐个点亮**；抵达后矩形内剩余
词条从宠物落点向外以波纹节奏快速补齐——不整块闪亮、没有无意义的绕圈、也不漏标。
每次接触标记时，词条旁边会弹出「已抓取」小提示窗，约 1 秒后渐隐。

设计原则（写联动逻辑前先读这一段）：

- **抓取是"指向性"的**：只有显式调用抓取接口才会采集；平时宠物随机游走、路过不抓取；
  用户或自动化点击页面时宠物会冲刺过去互动（同样不抓取）。
- **纯视觉、零干扰**：不带任何 UI 挂件；高亮通过 CSS Custom Highlight API 实现，不改动
  页面 DOM；特效层 `pointer-events:none`，绝不拦截点击；宠物从不代替页面滚动。
- **互动入口在页面里**：每个注入宠物的标签页右下角有一枚宠物徽标（宠物 glyph），点开
  菜单即可召唤到页面中央 / 开心庆祝 / 撒一把粒子；Esc 或点击空白处收起。菜单动作与
  工作台「宠物」页按钮完全一致，无需切回工作台窗口；控件随 `destroy()` 一并移除。
- **多帧词采，覆盖 iframe**：顶层文档之外，worker 会把轻量「词采器」懒注入到页面全部
  子帧（经 Playwright/CDP，**跨域 iframe 同样生效**），并把主视口矩形换算成帧内坐标
  逐帧采集。`fetch_* / harvest_*` 返回的采集数是顶层 + 全部帧的合计；帧内只扫描/高亮/
  计数，粒子与涟漪等动画仍由顶层宠物绘制。
- **自动注入**：主框架每次页面导航后自动（重新）注入宠物，插件**不需要**自己注入。
- **静默降级**：特效被关闭（宠物页开关 / 总览页"眼睛"，存于 `runtime/workbench.json`）
  或页面无法注入（`chrome://` 等内部页）时，下列所有方法**直接返回 `None`，不抛异常**
  ——插件代码无需任何判断或 try/except，放心直接调用。

---

## 一、Worker 侧方法（插件代码主要用这一组）

在插件 worker（`register(worker)` 里的 `worker`，或自动化类里的 `self.worker`）上直接调用。
这些方法内部走 Playwright，**只能在 worker 线程调用**（即你的命令 handler、worker 方法内），
不要在 Tkinter 回调里直接调——需要时请 `worker.submit(...)` 走命令。

约定：`page` 是 Playwright 的 `Page`；所有坐标都是**视口坐标**（与
`locator.bounding_box()` 返回值同一坐标系，宠物生活在可视区内、从不滚动页面）。
iframe 内的元素同样适用：Playwright 的 `bounding_box()` 返回主帧视口坐标，帧偏移
由 worker 侧自动换算，插件无需关心元素在不在 iframe 里。

### 1. 指向性接触采集（核心接口）

```python
worker.spider_fetch_element(page, *, selector=None, element=None, box=None,
                            padding=8, wait="auto")
```

宠物冲刺到目标元素，**抵达后**原地采集元素矩形内的文字（顶层文档 + 全部 iframe
一起结算）。这是插件"要读取信息"时的标准动作：先触发它，再做你的提取脚本，
形成"宠物爬过去抓取信息"的配合。

| 参数 | 说明 |
|---|---|
| `element` | Playwright `Locator`（推荐，如 `page.locator("#b_results")`） |
| `selector` | CSS 选择器（取第一个匹配元素） |
| `box` | 已经拿到的 `bounding_box()` 字典 `{x, y, width, height}` |
| `padding` | 采集矩形向外扩的边距，默认 8px |
| `wait` | **触发后等多久再返回**，见下文「触发后要等多久」，默认 `"auto"` |

三选一传入目标；都解析不到（元素不存在/不可见）返回 `None`。
返回 `{...state, tx, ty}`：`tx/ty` 是**钳制进可视区后的实际冲刺目标**（目标可能在
屏幕外，宠物只会冲到可视区边缘为止），需要等待到达时请以 `tx/ty` 为准。

```python
worker.spider_fetch_text(page, pattern, padding=10, wait="auto")
```

按**文本内容**定位的指向性采集：自动找到"最内层、可见、文本匹配 `pattern`（正则，
忽略大小写）且文本不超过 60 字符"的元素（多个命中取文本最短的），宠物爬过去接触采集。
适合"页面上有个积分数字/标题，宠物爬到它上面"的场景：

```python
worker.spider_fetch_text(page, r"积分|points")   # 微软积分获取信息时就是这样触发的
```

顶层文档找不到匹配时会**继续到全部 iframe（含跨域帧）里查找**，命中后把帧内坐标换算
回主视口坐标再冲刺采集。

> 长文本块（如整段新闻正文）匹配不到文本定位器（60 字符上限），请改用
> `spider_fetch_element(selector="正文容器")`。

### 2. 纯冲刺互动（不抓取）

```python
worker.spider_dash_to(page, x=None, y=None, *, selector=None, element=None,
                      box=None, wait=False)
```

宠物冲刺到目标处看一眼，然后继续游走。适合"自动化即将点击某处，让宠物抢先跑过去"
的互动效果。另外：**任何真实点击（用户或 Playwright 的 `mouse.click` /
`locator.click`）都会自动触发宠物冲刺过去**，这一条不需要你写任何代码。

### 3. 原地立即采集（不移动）

```python
worker.spider_harvest_element(page, *, selector=None, element=None, box=None, padding=8)
worker.spider_harvest_point(page, x, y, radius=150)
```

不移动，立刻采集目标矩形 / 半径内的词条并返回数量（**顶层 + 全部 iframe 合计**，
帧内高亮由词采器完成）。宠物已经就在目标附近、或不需要"爬过去"的动画时用这两个。

### 4. 氛围特效与控制

```python
worker.spider_burst(page, x=None, y=None, count=14)   # 粒子迸发（缺省在宠物当前位置）
worker.spider_ring(page, x=None, y=None)              # 涟漪一圈
worker.spider_celebrate(page)                          # 原地转圈庆祝：任务/数据完成时刻
worker.spider_wander(page)                             # 立刻恢复随机游走（会取消未完成的指向性抓取）
```

---

## 触发后要等多久？（`wait` 参数）

宠物爬到目标需要时间（冲刺速度与剩余距离成正比，340→950 px/s，跨屏约 1.2 秒）。
**如果触发完立刻读数据/跳转页面，宠物还没爬到，效果就断了**。所以所有"会移动"的
接口都带 `wait` 参数，由接口替你等：

| 取值 | 行为 | 适用场景 |
|---|---|---|
| `"auto"`（`fetch_*` 的默认值） | **按冲刺距离自动计算**：约 450ms 起步 + 每像素 1ms，500~2600ms 之间；随后继续轮询 `state().fetching`（最多补等 4.2 秒），等到**冲刺途中逐词标记 + 抵达后波纹补齐**（整体约 1~2.5 秒）与收尾特效播完再返回 | 采集信息：先等宠物爬到/采完，再读数据 |
| `True` | 等待宠物抵达 | 同 auto |
| `400`（数字，毫秒） | 精确等待指定毫秒 | 点击前的抢先冲刺：让宠物先跑 0.3~0.5 秒再点击 |
| `False` / `0`（`dash_to` 的默认值） | 不等待，触发后立即返回 | 纯氛围，不阻塞自动化节奏 |

自动等待的换算：`等待ms = 450 + 冲刺距离px`（距离越远等得越久，500~2600ms 封顶）。
等待期间 worker 线程原地小憩，不阻塞 Tkinter，也不影响页面。

> 内置流程已经全部接好自动等待：Rewards 读取积分信息、必应搜索/新闻结果页采集、
> 每日任务点击前抢先冲刺（450ms）。插件里通常只需要 `spider_fetch_element(...)`
> 一行，不用自己写 `wait_for_timeout`。

---

## 二、页面内 JS API（`window.__wbs`，进阶）

Worker 侧方法都是对它的封装；一般插件用不到，但做精细联动时可以在
`page.evaluate` 里直接调用（坐标同样是视口坐标）：

| 方法 | 说明 |
|---|---|
| `fetchRect(x, y, w, h)` | 指向性抓取：径直冲刺到目标矩形，**途中爬过谁就点亮谁**，抵达后从落点向外波纹补齐剩余词条，返回数量 |
| `fetchAt(x, y, r)` | 指向性抓取：半径 r 的圆形区域 |
| `dashTo(x, y)` | 纯冲刺移动（不抓取） |
| `harvestRect(x,y,w,h)` / `harvestAt(x,y,r)` | 原地立即采集顶层词条，返回数量（iframe 词条由 worker 侧词采器另行结算） |
| `burst(x, y, n)` / `ring(x, y)` | 粒子 / 涟漪（坐标传 `null` 用宠物当前位置） |
| `celebrate()` | 转圈庆祝约 1.2 秒 |
| `wander()` | 立刻换随机游走目标 |
| `addCollected(n)` | 记账：把帧词采器的采集数并入本页 `collected` 统计 |
| `state()` | `{x, y, px, py, speed, words, collected, fetching, vw, vh, pet}` |
| `rewordify()` | 增量补扫页面词条（SPA 内容变化 / 新展开的抽屉后刷新高亮目标，已标记状态保留） |
| `destroy()` | 完整移除特效 |

`state()` 字段：`x/y` 视口坐标、`px/py` 页面坐标、`speed` 当前速度、`words` 顶层词条总数、
`collected` 本页已采集数（含帧词采器记账）、`fetching` 指向性抓取及其**收尾特效**是否
执行中（冲刺途中逐词标记 + 抵达后波纹补齐约播 1~2.5 秒，播完回落为 `False`）、
`vw/vh` 视口尺寸、`pet` 当前宠物 ID。

各子帧（含跨域 iframe）内另有轻量词采器 `window.__wbs_collector`：
`harvestRect(x,y,w,h,max)` / `words()` / `sync()` / `destroy()`——只扫描、高亮与计数，
不做任何动画；由 worker 在调用 `fetch_* / harvest_*` 时自动懒注入与结算，插件无需操作。

行为细节：

- **冲刺速度与剩余距离成正比**（340→950 px/s 封顶，乘以宠物速度系数），跨屏约 1.2 秒到达；
  冲刺时转向速率加倍、且速度随朝向对齐度缩放——背对/侧对目标时先原地减速掉头再直线
  冲刺，不会绕出大弧线；
- 冲刺到目标 26px 内即视为**抵达**：结算采集 → 停留约 0.6 秒 → 继续游走；
  目标一直到不了则约 4.5~6.5 秒后在可达位置结算；
- **冲刺途中逐词标记**：抓取进行时，宠物接触半径（约 60px）内、且位于目标矩形内的
  词条随爬行逐个点亮（每帧限量，保持"爬过谁点亮谁"的观感）；矩形外的词条绝不误标；
- **抵达后波纹补齐**：矩形内剩余词条按离宠物落点的距离由近及远快速波纹点亮（整体约
  0.8 秒量级，词条越多每帧越多），不整块闪亮；视口外/滚动后可见的词条同样补齐，
  超时兜底就地补完，保证不漏标；
- **「已抓取」小提示窗**：每次接触标记词条时，在文字旁边弹出一个小提示窗（宠物主色
  描边，含词条文本预览），约 1 秒渐隐后移除；密集标记时自动节流避免刷屏；
- **抽屉/弹层内容同样可标记**：每次抓取开始与落地时按 DOM 签名**增量补扫**新出现的
  词条（如点击后才展开的抽屉内容），已标记的高亮与统计全部保留；抓取期间测量节奏
  自动加密（约 1 秒一次），抽屉滑入动画也不会导致标记错位；
- 同一宠物重复注入只会增量重扫词条（DOM 没变时完全无副作用、保留采集统计）；
  **换了宠物**（`__wbs.pet` 不同）则销毁旧实例、按新配色/造型全新初始化；
  页面跳转后自动全新初始化。

---

## 二·五、宠物系统与切换

- **注册表**：主程序 `PETS` 元组定义上架的宠物（id、名称、emoji、造型 body、
  主色 accent、速度 speed、描述）。当前只有 `spider`；引擎已参数化
  （`body: spider/beetle/bee` 造型 + 任意主色 + 速度系数），加新宠物只需加一项配置。
- **宠物页**：左侧菜单「宠物」页提供宠物卡片（应用/使用中）、主开关（与总览页
  "眼睛"图标同步）和互动按钮（召唤到页面中央 / 开心庆祝 / 撒一把粒子）。
- **切换生效**：选择宠物 → worker 命令 `spider_pet` → 写回 `workbench.json`
  的 `"spider_pet"` → 对当前打开的所有标签页立即重新注入；同宠物注入走轻量重扫，
  换宠物销毁重建。设置持久化，重启后保持。

---

## 三、完整示例

```python
"""plugins/my_plugin/worker.py"""
import time


def register(worker):
    def fetch_list(**payload):
        page = worker._current_page()
        worker._goto_page(page, "https://example.com/list")

        # 1) 信息读取前：让宠物爬到列表上，抵达后接触采集
        #    wait="auto" 会按冲刺距离自动等待（500~2600ms），宠物爬到后才继续往下执行
        worker.spider_fetch_element(page, selector="#result-list")

        data = page.locator("#result-list").inner_text()

        # 2) 文本定位变体：宠物自动爬到含"总计"二字的元素上采集
        worker.spider_fetch_text(page, r"总计|total")

        # 3) 点击前的抢先冲刺：只让宠物先跑 0.4 秒，不必等到抵达
        worker.spider_dash_to(page, selector="#next-page", wait=400)
        page.locator("#next-page").click()

        # 4) 完成：转圈庆祝 + 涟漪
        worker.spider_celebrate(page)

        worker.emit("my_result", data=data[:200])

    worker.command_handlers["fetch_list"] = fetch_list
```

时序建议：

- `spider_fetch_*` 默认 `wait="auto"`，已经替你等到宠物抵达——**不要为了等它自己再写
  `wait_for_timeout`**；只有下一步是"立刻跳转页面"且想让采集画面多停留一会时，才手动
  再补一小段等待；
- 采集高亮是**每页独立**的：页面跳转后重新注入、采集数清零，这是预期行为；
- 不要在循环里高频连发 `fetch`：宠物一次只背一个指向性抓取任务，后一次会覆盖前一次；
- 需要判断宠物是否到达时，用返回值里的 `tx/ty` 对比 `state()` 的 `x/y`，或直接看
  `state()["collected"]` 是否增长、`state()["fetching"]` 是否回落为 `False`。

---

## 四、内置联动点（无需插件代码，自动生效）

| 场景 | 联动 |
|---|---|
| 任何真实点击（用户 / Playwright） | 宠物自动冲刺过去互动 |
| Rewards「获取全部数据 / 刷新全部数据」 | 宠物按文本爬到积分数字上接触采集，读取前稍作停顿；成功后转圈庆祝 |
| 必应搜索、搜索任务的新闻结果页 | 宠物爬到 `#b_results` 结果区接触采集 |
| 每日任务逐个点击未完成项 | 宠物抢先冲刺到按钮上（pointerdown 自动触发） |
| 学习通「刷新全部数据」 | 宠物爬到个人昵称、课程列表区接触采集（iframe 内由词采器采集）；扫码登录成功、同步完成后转圈庆祝 |
| 学习通「开始学习」 | 点开章节前抢先冲刺到章节标题（跨帧词采同样生效）；视频任务冲刺到画面陪看、看完泛涟漪；文档/电子书任务前冲刺到内容区；章节学完粒子迸发；全部结束后转圈庆祝 |

## 五、开关与调试

- 开关：总览页标题行"眼睛"图标；持久化在 `runtime/workbench.json` 的 `"spider_overlay"`。
- 主工作台与各插件进程的 Edge 都会生效；`chrome://`、`edge://` 等内部页自动跳过。
- 调试：在 worker 命令里 `page.evaluate("() => window.__wbs.state()")` 查看位置 /
  采集数 / `fetching` 状态；特效未开启时所有调用返回 `None`。
