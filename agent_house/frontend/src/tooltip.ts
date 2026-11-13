// Tooltips for any element with data-tip: hover, keyboard focus, or a tap on an .info button.
// One fixed-position element, so cards, tables and the HA frame never clip it.
export function installTooltips(tip: HTMLElement) {
  let owner: HTMLElement | null = null;
  const show = (el: HTMLElement) => {
    if (!el.dataset.tip) return;
    owner = el;
    tip.textContent = el.dataset.tip;
    tip.hidden = false;
    const r = el.getBoundingClientRect();
    const x = Math.max(8, Math.min(r.left + r.width / 2 - tip.offsetWidth / 2, innerWidth - tip.offsetWidth - 8));
    const above = r.top - tip.offsetHeight - 8;
    tip.style.left = `${x}px`;
    tip.style.top = `${above < 8 ? r.bottom + 8 : above}px`;
    requestAnimationFrame(() => tip.classList.add('on'));
  };
  const hide = () => {
    owner = null;
    tip.classList.remove('on');
    tip.hidden = true;
  };
  const target = (e: Event, selector: string) => (e.target as Element | null)?.closest?.<HTMLElement>(selector) ?? null;
  document.addEventListener('pointerover', (e) => {
    const el = target(e, '[data-tip]');
    if (el && el !== owner) show(el);
  });
  document.addEventListener('pointerout', (e) => {
    if (owner && !owner.contains(e.relatedTarget as Node | null)) hide();
  });
  document.addEventListener('focusin', (e) => {
    const el = target(e, '[data-tip]');
    if (el) show(el);
  });
  document.addEventListener('focusout', hide);
  document.addEventListener('click', (e) => {
    const el = target(e, '.info[data-tip]');
    if (!el) return;
    e.preventDefault();
    if (owner === el) hide();
    else show(el);
  });
  addEventListener('scroll', hide, true);
}
