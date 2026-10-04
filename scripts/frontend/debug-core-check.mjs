#!/usr/bin/env node
/**
 * Debug 核心纯函数层断言脚本
 *
 * 环境说明：沿用 time-moment-check.mjs 同款模式——node 无法直接 import ts，
 * 用项目自带 tsc 将 debugTypes.ts + debugCore.ts 编译为 CommonJS 到 os.tmpdir() 临时目录，
 * require 加载**真实实现**后断言（非复制公式，避免几何逻辑双份漂移）。
 * DOM 隔离约定：几何函数只接受 Rect / mock Element（带 getBoundingClientRect 的最小桩）。
 * 执行：node scripts/debug-core-check.mjs
 */
import { execFileSync } from 'node:child_process'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { createRequire } from 'node:module'

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..', 'frontend')
const tscBin = path.join(frontendRoot, 'node_modules', '.bin', 'tsc')

// 编译 debugTypes.ts + debugCore.ts（--ignoreConfig：避免 TS5112「tsconfig 存在但命令行指定文件时不加载」）
const outDir = mkdtempSync(path.join(tmpdir(), 'sr-debug-core-'))
try {
  execFileSync(tscBin, [
    'src/utils/debug/debugTypes.ts', 'src/utils/debug/debugCore.ts',
    '--ignoreConfig', '--outDir', outDir,
    '--module', 'commonjs', '--target', 'es2022', '--skipLibCheck',
  ], { cwd: frontendRoot, stdio: 'pipe' })
} catch (err) {
  rmSync(outDir, { recursive: true, force: true })
  throw err
}

const require = createRequire(import.meta.url)

/* getBoxQuads 路径测试前置：模块级能力检测 HAS_GET_BOX_QUADS 在 require 时求值，node 无 Element，注入最小桩。
   注意：mockEl 均为普通对象（原型链不含 Element.prototype），注入不影响回退测试的「无 getBoxQuads」语义；
   能力移除场景由 ⑥-7 删 prototype 方法 + readBoxQuads 动态判定覆盖。 */
if (typeof globalThis.Element === 'undefined') {
  globalThis.Element = class Element {}
  Element.prototype.getBoxQuads = () => []
}

const core = require(path.join(outDir, 'debugCore.js'))

let passed = 0
let failed = 0
/** 断言辅助：输出 ✓/✗ 并计数，extra 用于失败时打印现场 */
function assert(name, cond, extra) {
  if (cond) {
    passed++
    console.log(`  ✓ ${name}`)
  } else {
    failed++
    console.error(`  ✗ ${name}${extra !== undefined ? ` — 现场:${JSON.stringify(extra)}` : ''}`)
  }
}

/** 构造归一 Rect */
const r = (left, top, right, bottom) => ({ left, top, right, bottom, width: right - left, height: bottom - top })

/** 最小 Element 桩：仅实现 describe/rectOf 依赖的字段 */
const mockEl = (rect, opts = {}) => ({
  tagName: opts.tagName ?? 'DIV',
  id: opts.id ?? '',
  classList: opts.classList ?? [],
  getBoundingClientRect: () => rect,
})

console.log('① rectsIntersect：相交 / 不相交 / 内含')
{
  // 相交：A 左上角与 B 交叠 5x5，面积 25，较小者 A(100) → areaPct 0.25
  const hit = core.rectsIntersect(r(0, 0, 10, 10), r(5, 5, 15, 15))
  assert('相交命中', hit.hit === true)
  assert('相交 overlap 矩形', hit.overlap && hit.overlap.left === 5 && hit.overlap.top === 5
    && hit.overlap.right === 10 && hit.overlap.bottom === 10 && hit.overlap.width === 5 && hit.overlap.height === 5, hit)
  assert('相交 areaPct=交叠/较小者(0.25)', hit.areaPct === 0.25, hit)

  // 不相交：分离 10px 间隙
  const miss = core.rectsIntersect(r(0, 0, 10, 10), r(20, 20, 30, 30))
  assert('不相交未命中', miss.hit === false && miss.overlap === null && miss.areaPct === 0, miss)

  // 内含：B 完全在 A 内 → overlap=B，areaPct=1
  const inside = core.rectsIntersect(r(0, 0, 100, 100), r(25, 25, 75, 75))
  assert('内含命中', inside.hit === true)
  assert('内含 overlap=B', inside.overlap && inside.overlap.left === 25 && inside.overlap.right === 75
    && inside.overlap.top === 25 && inside.overlap.bottom === 75, inside)
  assert('内含 areaPct=1', inside.areaPct === 1, inside)

  // 边相切（共边）不算相交
  const touch = core.rectsIntersect(r(0, 0, 10, 10), r(10, 0, 20, 10))
  assert('边相切不算相交', touch.hit === false && touch.areaPct === 0, touch)
}

console.log('② viewportOverflow：四方向 / 部分溢出')
{
  // 四方向全部溢出：left -10、top -5、right 110>100、bottom 105>100
  const four = core.viewportOverflow(mockEl(r(-10, -5, 110, 105)), 100, 100)
  assert('四方向溢出各 1 条', four.length === 4, four)
  assert('left 溢出 10px', four.some((o) => o.dir === 'left' && o.px === 10), four)
  assert('top 溢出 5px', four.some((o) => o.dir === 'top' && o.px === 5), four)
  assert('right 溢出 10px', four.some((o) => o.dir === 'right' && o.px === 10), four)
  assert('bottom 溢出 5px', four.some((o) => o.dir === 'bottom' && o.px === 5), four)

  // 仅 left 溢出：-8 < 0，其余均在 0..100 内
  const partial = core.viewportOverflow(mockEl(r(-8, 2, 98, 92)), 100, 100)
  assert('仅 left 溢出', partial.length === 1 && partial[0].dir === 'left' && partial[0].px === 8, partial)

  // 完全在视口内 → 无溢出
  const inside = core.viewportOverflow(mockEl(r(10, 10, 90, 90)), 100, 100)
  assert('视口内无溢出', inside.length === 0, inside)

  // describe 描述格式 tag.cls#id
  const described = core.viewportOverflow(mockEl(r(-5, 0, 50, 20), { tagName: 'SECTION', id: 'app', classList: ['panel'] }), 100, 100)
  assert('describe=tag.cls#id', described[0].el === 'section.panel#app', described)
}

console.log('③ boxModelOf 回退路径：margin/border/padding/content 四层分解（mock 计算样式，无 getBoxQuads）')
{
  const savedCS = globalThis.getComputedStyle
  // 计算样式桩：margin 10/5/8/6，border 1/2/3/4，padding 5/6/7/8
  globalThis.getComputedStyle = () => ({
    marginLeft: '10px', marginTop: '5px', marginRight: '8px', marginBottom: '6px',
    borderLeftWidth: '1px', borderTopWidth: '2px', borderRightWidth: '3px', borderBottomWidth: '4px',
    paddingLeft: '5px', paddingTop: '6px', paddingRight: '7px', paddingBottom: '8px',
  })
  try {
    // border box = getBoundingClientRect：left 10, top 10, w 100, h 50
    const bm = core.boxModelOf(mockEl(r(10, 10, 110, 60)))

    // margin：向外扩
    assert('margin 矩形', bm.margin.rect.left === 0 && bm.margin.rect.top === 5 && bm.margin.rect.right === 118
      && bm.margin.rect.bottom === 66 && bm.margin.rect.width === 118 && bm.margin.rect.height === 61, bm.margin.rect)
    // border = border box
    assert('border 矩形=bbox', bm.border.rect.left === 10 && bm.border.rect.top === 10 && bm.border.rect.right === 110
      && bm.border.rect.bottom === 60 && bm.border.rect.width === 100 && bm.border.rect.height === 50, bm.border.rect)
    // padding：border 内缩 border 宽
    assert('padding 矩形', bm.padding.rect.left === 11 && bm.padding.rect.top === 12 && bm.padding.rect.right === 107
      && bm.padding.rect.bottom === 56 && bm.padding.rect.width === 96 && bm.padding.rect.height === 44, bm.padding.rect)
    // content：padding 内缩 padding 宽
    assert('content 矩形', bm.content.rect.left === 16 && bm.content.rect.top === 18 && bm.content.rect.right === 100
      && bm.content.rect.bottom === 48 && bm.content.rect.width === 84 && bm.content.rect.height === 30, bm.content.rect)
    assert('回退 transformed=false', bm.transformed === false, bm)
  } finally {
    globalThis.getComputedStyle = savedCS
  }
}

console.log('④ snapToEdges：吸附与 guide / 阈值参数 / 不吸附')
{
  const targets = [r(0, 0, 100, 50)]

  // x=3 距 left 3px ≤4 → 吸附 x=0；y=48 距 bottom 2px → 吸附 y=50
  const snap = core.snapToEdges({ x: 3, y: 48 }, targets)
  assert('吸附到 left', snap.x === 0, snap)
  assert('吸附到 bottom', snap.y === 50, snap)
  assert('guides 含 left/bottom', snap.guides.length === 2
    && snap.guides.some((g) => g.dir === 'left' && g.value === 0)
    && snap.guides.some((g) => g.dir === 'bottom' && g.value === 50), snap.guides)

  // 垂直中线吸附：x=51 距 vmid(50) 1px → 吸附 50
  const vmid = core.snapToEdges({ x: 51, y: 20 }, targets)
  assert('吸附 vmid', vmid.x === 50 && vmid.guides.some((g) => g.dir === 'vmid' && g.value === 50), vmid)

  // 水平中线吸附：y=24 距 hmid(25) 1px → 吸附 25
  const hmid = core.snapToEdges({ x: 20, y: 24 }, targets)
  assert('吸附 hmid', hmid.y === 25 && hmid.guides.some((g) => g.dir === 'hmid' && g.value === 25), hmid)

  // 阈值参数：x=7 距 left 7px，默认 4 不吸附；threshold=8 时吸附
  const tight = core.snapToEdges({ x: 7, y: 20 }, targets)
  assert('默认阈值 4 不吸附', tight.x === 7 && tight.guides.length === 0, tight)
  const loose = core.snapToEdges({ x: 7, y: 20 }, targets, 8)
  assert('threshold=8 吸附', loose.x === 0 && loose.guides.some((g) => g.dir === 'left'), loose)

  // 远离所有边 → 原样返回
  const far = core.snapToEdges({ x: 200, y: 200 }, targets)
  assert('远离不吸附', far.x === 200 && far.y === 200 && far.guides.length === 0, far)

  // 多目标：吸附到第二个矩形的 right
  const multi = core.snapToEdges({ x: 158, y: 20 }, [r(0, 0, 100, 50), r(120, 10, 160, 60)])
  assert('多目标吸附 right', multi.x === 160 && multi.guides.some((g) => g.dir === 'right' && g.value === 160), multi)
}

console.log('⑤ breakpointCross：相交 / 多交点 / 边界不算 / 无交点')
{
  // 760..780 跨越 768
  const one = core.breakpointCross(mockEl(r(760, 0, 780, 10)))
  assert('单一断点穿越', one.length === 1 && one[0].bp === 768, one)

  // 400..600 跨越 480 与 576 两个断点
  const two = core.breakpointCross(mockEl(r(400, 0, 600, 10)))
  assert('多断点穿越', two.length === 2 && two.some((c) => c.bp === 480) && two.some((c) => c.bp === 576), two)

  // 左边界恰等于断点（left=480 边界相切不算穿越；480..500 内无其他断点）
  const edge = core.breakpointCross(mockEl(r(480, 0, 500, 10)))
  assert('边界相切不算穿越', edge.length === 0, edge)

  // 完全在断点间隙内
  const none = core.breakpointCross(mockEl(r(100, 0, 200, 10)))
  assert('断点间隙内无穿越', none.length === 0, none)
}

console.log('⑥ boxModelFromQuads/getBoxQuads：轴对齐 / 旋转 45° / 能力移除回退')
{
  // 轴对齐 DOMQuadLike 构造（rect 四角；与 ③ 回退几何同值）
  const quadLike = (rect) => ({
    p1: { x: rect.left, y: rect.top },
    p2: { x: rect.right, y: rect.top },
    p3: { x: rect.right, y: rect.bottom },
    p4: { x: rect.left, y: rect.bottom },
  })
  // 与 ③ 相同的四层几何：margin(0,5,118,66) / border(10,10,110,60) / padding(11,12,107,56) / content(16,18,100,48)
  const LAYER_RECTS = {
    margin: r(0, 5, 118, 66),
    border: r(10, 10, 110, 60),
    padding: r(11, 12, 107, 56),
    content: r(16, 18, 100, 48),
  }

  // ⑥-1 quadToBoxQuad 轴对齐：corners 顺序（左上/右上/右下/左下）+ 包围矩形=原 rect
  const aq = core.quadToBoxQuad(quadLike(LAYER_RECTS.content))
  assert('quadToBoxQuad 轴对齐 corners=左上/右上/右下/左下', aq.corners[0].x === 16 && aq.corners[0].y === 18
    && aq.corners[1].x === 100 && aq.corners[1].y === 18 && aq.corners[2].x === 100 && aq.corners[2].y === 48
    && aq.corners[3].x === 16 && aq.corners[3].y === 48, aq.corners)
  assert('quadToBoxQuad 轴对齐 rect=原 rect', aq.rect.left === 16 && aq.rect.top === 18 && aq.rect.right === 100
    && aq.rect.bottom === 48 && aq.rect.width === 84 && aq.rect.height === 30, aq.rect)

  // ⑥-2 旋转 45°：中心(50,50) 半宽 20 正方形；包围矩形=四角 min/max
  const S2 = Math.SQRT2 / 2
  const rotPt = (dx, dy) => ({ x: 50 + (dx - dy) * S2, y: 50 + (dx + dy) * S2 })
  const rotLike = { p1: rotPt(-20, -20), p2: rotPt(20, -20), p3: rotPt(20, 20), p4: rotPt(-20, 20) }
  const rotQ = core.quadToBoxQuad(rotLike)
  const round3 = (n) => Math.round(n * 1000) / 1000
  assert('quadToBoxQuad 旋转 45° 包围矩形=四角 min/max', round3(rotQ.rect.left) === round3(50 - 40 * S2)
    && round3(rotQ.rect.top) === round3(50 - 40 * S2) && round3(rotQ.rect.right) === round3(50 + 40 * S2)
    && round3(rotQ.rect.bottom) === round3(50 + 40 * S2) && round3(rotQ.rect.width) === round3(80 * S2), rotQ.rect)
  assert('quadToBoxQuad 旋转 corners 原样保留', rotQ.corners[0].x === rotLike.p1.x && rotQ.corners[0].y === rotLike.p1.y
    && rotQ.corners[3].x === rotLike.p4.x && rotQ.corners[3].y === rotLike.p4.y, rotQ.corners)
  assert('旋转 quad 非轴对齐(p1.y≠p2.y)', rotQ.corners[0].y !== rotQ.corners[1].y, rotQ.corners)

  // ⑥-3 boxModelFromQuads 纯函数：四层轴对齐 → transformed=false，rect 与 ③ 回退一致
  const axisModel = core.boxModelFromQuads(
    ['margin', 'border', 'padding', 'content'].map((k) => core.quadToBoxQuad(quadLike(LAYER_RECTS[k]))),
  )
  assert('boxModelFromQuads 轴对齐四层 rect 与回退一致', axisModel.margin.rect.left === 0
    && axisModel.border.rect.left === 10 && axisModel.padding.rect.left === 11 && axisModel.content.rect.left === 16
    && axisModel.content.rect.width === 84 && axisModel.content.rect.height === 30, axisModel)
  assert('boxModelFromQuads 轴对齐 transformed=false', axisModel.transformed === false, axisModel)

  // ⑥-4 boxModelFromQuads：content 层旋转 → transformed=true，角点原样保留
  const rotModel = core.boxModelFromQuads([
    core.quadToBoxQuad(quadLike(LAYER_RECTS.margin)),
    core.quadToBoxQuad(quadLike(LAYER_RECTS.border)),
    core.quadToBoxQuad(quadLike(LAYER_RECTS.padding)),
    rotQ,
  ])
  assert('boxModelFromQuads 含旋转层 transformed=true', rotModel.transformed === true, rotModel)
  assert('boxModelFromQuads 旋转层角点原样保留', rotModel.content.corners[1].x === rotLike.p2.x
    && rotModel.content.corners[2].x === rotLike.p3.x, rotModel.content)

  // ⑥-5 boxModelOf 优先路径：实例 getBoxQuads 返回四层轴对齐 quad → rect 与回退一致
  const gbEl = {
    ...mockEl(r(10, 10, 110, 60)),
    getBoxQuads: (opts) => [quadLike(LAYER_RECTS[opts.box])],
  }
  const viaQuads = core.boxModelOf(gbEl)
  assert('boxModelOf 优先 getBoxQuads：四层 rect 与回退一致', viaQuads.margin.rect.left === 0
    && viaQuads.border.rect.right === 110 && viaQuads.padding.rect.left === 11 && viaQuads.content.rect.left === 16
    && viaQuads.content.rect.height === 30, viaQuads)
  assert('boxModelOf 优先路径 transformed=false', viaQuads.transformed === false, viaQuads)

  // ⑥-6 boxModelOf 旋转实例：content 层旋转 → transformed=true + 包围矩形正确
  const rotEl = {
    ...mockEl(r(10, 10, 110, 60)),
    getBoxQuads: (opts) => (opts.box === 'content' ? [rotLike] : [quadLike(LAYER_RECTS[opts.box])]),
  }
  const viaRot = core.boxModelOf(rotEl)
  assert('boxModelOf 旋转实例 transformed=true', viaRot.transformed === true, viaRot)
  assert('boxModelOf 旋转实例 content 包围矩形正确', round3(viaRot.content.rect.left) === round3(50 - 40 * S2)
    && round3(viaRot.content.rect.top) === round3(50 - 40 * S2) && round3(viaRot.content.rect.width) === round3(80 * S2),
    viaRot.content.rect)

  // ⑥-7 能力移除（删 Element.prototype.getBoxQuads）→ readBoxQuads 动态判定失效 → boxModelOf 回退
  delete globalThis.Element.prototype.getBoxQuads
  const savedCS = globalThis.getComputedStyle
  globalThis.getComputedStyle = () => ({
    marginLeft: '10px', marginTop: '5px', marginRight: '8px', marginBottom: '6px',
    borderLeftWidth: '1px', borderTopWidth: '2px', borderRightWidth: '3px', borderBottomWidth: '4px',
    paddingLeft: '5px', paddingTop: '6px', paddingRight: '7px', paddingBottom: '8px',
  })
  try {
    const fb = core.boxModelOf(mockEl(r(10, 10, 110, 60)))
    assert('能力移除后 boxModelOf 回退（rect 与 ③ 一致）', fb.margin.rect.left === 0
      && fb.border.rect.left === 10 && fb.content.rect.left === 16 && fb.content.rect.right === 100, fb)
    assert('能力移除后回退 transformed=false', fb.transformed === false, fb)
  } finally {
    globalThis.getComputedStyle = savedCS
  }
}

rmSync(outDir, { recursive: true, force: true })

const total = passed + failed
console.log(`\n${passed} 用例通过` + (failed > 0 ? `，${failed} 用例失败` : ''))
process.exit(failed > 0 ? 1 : 0)
