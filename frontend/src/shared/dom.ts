/**
 * XSS-safe DOM helper utilities (never concatenates user or DB values into innerHTML).
 */

export function clearNode(node: Element): void {
  while (node.firstChild) {
    node.removeChild(node.firstChild);
  }
}

export function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs?: {
    className?: string;
    text?: string;
    id?: string;
    type?: string;
    value?: string;
    placeholder?: string;
    title?: string;
    disabled?: boolean;
    checked?: boolean;
    min?: string;
    max?: string;
    step?: string;
    rows?: number;
    ariaLabel?: string;
    onClick?: (ev: MouseEvent) => void;
    onChange?: (ev: Event) => void;
    onInput?: (ev: Event) => void;
  },
  children?: Array<Node | string | null | undefined>,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (attrs) {
    if (attrs.className) node.className = attrs.className;
    if (attrs.id) node.id = attrs.id;
    if (attrs.text !== undefined) node.textContent = attrs.text;
    if (attrs.title) node.title = attrs.title;
    if (attrs.ariaLabel) node.setAttribute("aria-label", attrs.ariaLabel);
    if ("type" in node && attrs.type) (node as HTMLInputElement).type = attrs.type;
    if ("value" in node && attrs.value !== undefined) (node as HTMLInputElement).value = attrs.value;
    if ("placeholder" in node && attrs.placeholder) (node as HTMLInputElement).placeholder = attrs.placeholder;
    if ("disabled" in node && attrs.disabled !== undefined) (node as HTMLButtonElement).disabled = attrs.disabled;
    if ("checked" in node && attrs.checked !== undefined) (node as HTMLInputElement).checked = attrs.checked;
    if ("min" in node && attrs.min !== undefined) (node as HTMLInputElement).min = attrs.min;
    if ("max" in node && attrs.max !== undefined) (node as HTMLInputElement).max = attrs.max;
    if ("step" in node && attrs.step !== undefined) (node as HTMLInputElement).step = attrs.step;
    if ("rows" in node && attrs.rows !== undefined) (node as HTMLTextAreaElement).rows = attrs.rows;
    if (attrs.onClick) {
      const handler = attrs.onClick;
      node.addEventListener("click", (ev: Event) => handler(ev as MouseEvent));
    }
    if (attrs.onChange) node.addEventListener("change", attrs.onChange);
    if (attrs.onInput) node.addEventListener("input", attrs.onInput);
  }
  if (children) {
    for (const child of children) {
      if (child === null || child === undefined) continue;
      if (typeof child === "string") {
        node.appendChild(document.createTextNode(child));
      } else {
        node.appendChild(child);
      }
    }
  }
  return node;
}

export function formatShortTime(iso: string): string {
  const dt = new Date(iso);
  if (Number.isNaN(dt.getTime())) return iso;
  return dt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
