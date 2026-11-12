import { createRollingText } from "@kitlangton/rolling-number";

const controllers = new WeakMap<
  HTMLElement,
  ReturnType<typeof createRollingText>
>();
const textMeasure = document.createElement("canvas").getContext("2d")!;
function fitMediaText(element: HTMLElement, text: string) {
  if (!element.matches(".wb-media h3, .wb-media p")) return text;
  element.title = text;
  const style = getComputedStyle(element);
  textMeasure.font = `${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
  const width = element.parentElement!.clientWidth;
  if (!width) return text;
  const spacing = parseFloat(style.letterSpacing) || 0;
  const measure = (value: string) =>
    textMeasure.measureText(value).width + [...value].length * spacing;
  if (measure(text) <= width) return text;
  const chars = [...text];
  let low = 0,
    high = chars.length;
  while (low < high) {
    const mid = Math.ceil((low + high) / 2);
    if (measure(chars.slice(0, mid).join("") + "…") <= width) low = mid;
    else high = mid - 1;
  }
  return chars.slice(0, low).join("") + "…";
}
export function rollText(
  element: HTMLElement,
  text: string,
  animated: boolean
) {
  const fullText = text;
  text = fitMediaText(element, text);
  let controller = controllers.get(element);
  if (!controller) {
    controller = createRollingText(element, {
      text,
      duration: 460,
      motionBlur: true,
      transition: "direct",
      stagger: "none",
      direction: "up",
      animated,
    });
    controllers.set(element, controller);
  } else controller.update({ text, animated });
  if (!animated) controller.finish();
  if (element.matches(".wb-media h3, .wb-media p"))
    element.setAttribute("aria-label", fullText);
}

/** Patch in place so media callbacks retain active reels, artwork and focus. */
export function patchRollingPanel(
  parent: HTMLElement,
  html: string,
  animated: boolean
) {
  const template = document.createElement("template");
  template.innerHTML = html;
  /** Stable node identity: a source child carrying `data-wb-key` adopts the live
   *  node with the same key wherever that node currently sits, so an insertion,
   *  a removal or a reorder can never hand a live node another region's role.
   *  Position alone is not identity: measured on the running build, the busy
   *  loading bar prepended at index 0 shifted every sibling, the positional
   *  matcher compared a `<div>` against a `<section>` and rebuilt the whole
   *  following list — the log list dropped scrollTop 200 → 0 and the model
   *  detail dropped 100 → 0, and the log detail title lost focus mid-read.
   *  Keys are only ever read as an ADDITIONAL identity on top of the existing
   *  position + tagName match: an unkeyed child keeps today's behaviour. */
  const keyOf = (node: Node): string | null =>
    node instanceof Element ? node.getAttribute("data-wb-key") : null;
  const patch = (target: Node, source: Node) => {
    // `Element`, NOT `HTMLElement`: an SVG element is an `SVGElement` and never
    // an `HTMLElement`, so an `HTMLElement` guard here silently skips every
    // SVG node and freezes its attributes at whatever the node was created with.
    // Measured on the running build: expanding one chart series left 13 of 14 tip
    // plates at their one-line size (83x27) while their text grew to two lines,
    // the 「区间合计」 label kept the 「末值」 label's x=988/text-anchor=end, and
    // 13 hotspot aria-labels lagged one render. Every SVG attribute in this
    // console is declarative (the render writes it; only CSS reveals a tip or a
    // crosshair, and the four runtime `setAttribute` calls all live outside
    // `.wb-content`), so syncing them from the new source is correct.
    if (target instanceof Element && source instanceof Element) {
      // A rolling reel is the ONE node whose attributes the reel library owns
      // after creation, so it keeps the narrower guard and its early return.
      if (
        target instanceof HTMLElement &&
        source instanceof HTMLElement &&
        source.hasAttribute("data-wb-roll")
      ) {
        if (!controllers.has(target))
          for (const attr of [...source.attributes])
            target.setAttribute(attr.name, attr.value);
        rollText(target, source.textContent ?? "", animated);
        return;
      }
      if (target instanceof HTMLElement && controllers.has(target)) {
        controllers.get(target)!.destroy();
        controllers.delete(target);
      }
      for (const attr of [...target.attributes])
        if (!source.hasAttribute(attr.name)) target.removeAttribute(attr.name);
      for (const attr of [...source.attributes])
        if (target.getAttribute(attr.name) !== attr.value)
          target.setAttribute(attr.name, attr.value);
    }
    const wanted = [...source.childNodes];
    // Pass 1 — adopt keyed nodes into their source position. A node already at
    // the right index is left exactly where it is (`insertBefore` with its own
    // next sibling is a no-op), so an unchanged region is never moved.
    const adopted = new Map<Node, string>();
    for (const [index, next] of wanted.entries()) {
      const key = keyOf(next);
      if (key === null) continue;
      const existing = [...target.childNodes].find(
        (node) => keyOf(node) === key && !adopted.has(node)
      );
      if (!existing) continue;
      adopted.set(existing, key);
      if (target.childNodes[index] !== existing)
        target.insertBefore(existing, target.childNodes[index] ?? null);
    }
    wanted.forEach((next, index) => {
      const key = keyOf(next);
      let current: ChildNode | undefined = target.childNodes[index];
      // A live node carrying a key is only ever reused by the slot carrying
      // that same key — never handed another region's role by position. An
      // unkeyed node keeps the plain positional match it always had.
      if (current && keyOf(current) !== key) current = undefined;
      if (!current || current.nodeName !== next.nodeName) {
        const fresh = next.cloneNode(false) as ChildNode;
        if (current) {
          dispose(current);
          target.replaceChild(fresh, current);
        } else target.insertBefore(fresh, target.childNodes[index] ?? null);
        current = fresh;
      }
      if (next.nodeType === Node.TEXT_NODE) {
        if (current.textContent !== next.textContent)
          current.textContent = next.textContent;
      } else patch(current, next);
    });
    while (target.childNodes.length > wanted.length) {
      dispose(target.lastChild!);
      target.lastChild!.remove();
    }
  };
  const dispose = (node: Node) => {
    if (node instanceof HTMLElement) {
      controllers.get(node)?.destroy();
      controllers.delete(node);
      node.querySelectorAll<HTMLElement>("[data-wb-roll]").forEach((el) => {
        controllers.get(el)?.destroy();
        controllers.delete(el);
      });
    }
  };
  patch(parent, template.content);
}
