"use strict";

// Keep the select as the application's value/options model. The visible controls
// use page styles, including the popup, instead of the platform's native menu.
window.PlaygroundChoices = (() => {
  const controls = new Map();
  let opened = null;

  class Choice {
    constructor(select) {
      this.select = select;
      this.segmented = select.dataset.control === "segments";
      this.key = "";
      this.active = 0;
      this.prefix = "";
      this.typedAt = 0;
      this.root = document.createElement("div");
      this.root.className = this.segmented ? "choice-segments" : "choice-control";
      select.before(this.root);
      this.root.append(select);
      select.hidden = true;
      select.tabIndex = -1;
      select.setAttribute("aria-hidden", "true");
      this.label = select.getAttribute("aria-label") || select.id;
      if (this.segmented) {
        this.root.setAttribute("role", "radiogroup");
        this.root.setAttribute("aria-label", this.label);
      } else {
        this.trigger = document.createElement("button");
        this.trigger.type = "button";
        this.trigger.id = select.id + "-trigger";
        this.trigger.className = "choice-trigger";
        this.trigger.setAttribute("role", "combobox");
        this.trigger.setAttribute("aria-label", this.label);
        this.trigger.setAttribute("aria-haspopup", "listbox");
        this.trigger.setAttribute("aria-expanded", "false");
        this.text = document.createElement("span");
        this.text.className = "choice-value";
        const arrow = document.createElement("span");
        arrow.className = "choice-arrow";
        arrow.setAttribute("aria-hidden", "true");
        this.trigger.append(this.text, arrow);
        this.root.append(this.trigger);
        this.popup = document.createElement("div");
        this.popup.id = select.id + "-listbox";
        this.popup.className = "choice-popup";
        this.popup.hidden = true;
        this.popup.setAttribute("role", "listbox");
        this.popup.setAttribute("aria-label", this.label);
        this.trigger.setAttribute("aria-controls", this.popup.id);
        document.body.append(this.popup);
        for (const label of document.querySelectorAll(`label[for="${select.id}"]`)) {
          label.htmlFor = this.trigger.id;
        }
        this.trigger.onclick = () => opened === this ? this.close() : this.open();
        this.trigger.onkeydown = e => this.keydown(e);
        this.popup.onpointerdown = e => e.preventDefault();
      }
      select.addEventListener("change", () => this.sync());
      new MutationObserver(() => this.sync()).observe(select, {
        childList: true, subtree: true, attributes: true,
        attributeFilter: ["disabled", "title", "selected"],
      });
      this.sync();
    }

    sync() {
      const options = [...this.select.options];
      const key = JSON.stringify(options.map(o => [o.value, o.text, o.disabled, o.title]));
      if (key !== this.key) {
        const focusedValue = this.root.querySelector(":focus")?.dataset.value;
        this.key = key;
        const container = this.segmented ? this.root : this.popup;
        for (const item of container.querySelectorAll(".choice-item")) item.remove();
        options.forEach((option, index) => {
          const item = document.createElement(this.segmented ? "button" : "div");
          item.className = "choice-item";
          item.dataset.value = option.value;
          item.id = this.select.id + "-option-" + index;
          item.title = option.title || option.text;
          item.setAttribute("role", this.segmented ? "radio" : "option");
          item.textContent = option.text;
          if (this.segmented) {
            item.type = "button";
            item.onclick = () => this.choose(index);
            item.onkeydown = e => {
              let next = index;
              if (e.key === "ArrowRight" || e.key === "ArrowDown") next++;
              else if (e.key === "ArrowLeft" || e.key === "ArrowUp") next--;
              else if (e.key === "Home") next = 0;
              else if (e.key === "End") next = options.length - 1;
              else return;
              e.preventDefault();
              const target = this.available(next, next < index ? -1 : 1);
              this.choose(target);
              this.items[target]?.focus();
            };
          } else {
            item.onpointermove = () => this.highlight(index, false);
            item.onclick = () => this.choose(index);
          }
          container.append(item);
        });
        this.items = [...container.querySelectorAll(".choice-item")];
        if (focusedValue !== undefined) this.items.find(i => i.dataset.value === focusedValue)?.focus();
      }
      const selected = this.select.selectedIndex;
      this.items.forEach((item, index) => {
        const disabled = this.select.disabled || options[index].disabled;
        item.setAttribute(this.segmented ? "aria-checked" : "aria-selected", String(index === selected));
        item.setAttribute("aria-disabled", String(disabled));
        if (this.segmented) {
          item.disabled = disabled;
          item.tabIndex = index === selected ? 0 : -1;
        }
      });
      if (!this.segmented) {
        this.text.textContent = options[selected]?.text || "Select…";
        this.trigger.disabled = this.select.disabled || !options.length;
        this.trigger.title = this.select.title || options[selected]?.text || this.label;
        if (this.trigger.disabled) this.close();
        else if (opened === this) {
          this.highlight(Math.min(this.active, options.length - 1), false);
          this.position();
        }
      }
    }

    available(index, direction) {
      const length = this.select.options.length;
      for (let n = 0; n < length; n++, index += direction) {
        const candidate = (index % length + length) % length;
        if (!this.select.options[candidate].disabled) return candidate;
      }
      return -1;
    }

    choose(index) {
      if (this.select.disabled || !this.select.options[index] || this.select.options[index].disabled) return;
      const value = this.select.options[index].value;
      const changed = value !== this.select.value;
      this.select.value = value;
      this.close();
      this.sync();
      if (changed) this.select.dispatchEvent(new Event("change", {bubbles: true}));
    }

    position() {
      const rect = this.trigger.getBoundingClientRect(), gap = 6, margin = 8;
      const width = Math.min(Math.max(rect.width, 260), window.innerWidth - margin * 2);
      this.popup.style.width = width + "px";
      this.popup.style.left = Math.max(margin, Math.min(rect.left, window.innerWidth - width - margin)) + "px";
      const below = window.innerHeight - rect.bottom - margin - gap;
      const above = rect.top - margin - gap;
      const upwards = below < 180 && above > below;
      this.popup.style.maxHeight = Math.max(60, Math.min(320, upwards ? above : below)) + "px";
      this.popup.style.top = upwards ? "auto" : rect.bottom + gap + "px";
      this.popup.style.bottom = upwards ? window.innerHeight - rect.top + gap + "px" : "auto";
    }

    open() {
      if (this.trigger.disabled) return;
      opened?.close();
      opened = this;
      this.popup.hidden = false;
      this.trigger.setAttribute("aria-expanded", "true");
      this.position();
      this.highlight(this.available(Math.max(0, this.select.selectedIndex), 1));
    }

    close() {
      if (opened !== this) return;
      opened = null;
      this.popup.hidden = true;
      this.trigger.setAttribute("aria-expanded", "false");
      this.trigger.removeAttribute("aria-activedescendant");
      this.prefix = "";
    }

    highlight(index, scroll = true) {
      if (!this.select.options[index] || this.select.options[index].disabled) return;
      this.active = index;
      this.items.forEach((item, i) => item.classList.toggle("active", i === index));
      this.trigger.setAttribute("aria-activedescendant", this.items[index].id);
      // Scroll only the menu, keeping the page and trigger in place.
      if (scroll) {
        const item = this.items[index];
        if (item.offsetTop < this.popup.scrollTop) this.popup.scrollTop = item.offsetTop;
        else if (item.offsetTop + item.offsetHeight > this.popup.scrollTop + this.popup.clientHeight) {
          this.popup.scrollTop = item.offsetTop + item.offsetHeight - this.popup.clientHeight;
        }
      }
    }

    keydown(e) {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "Tab") { this.close(); return; }
      if (e.key === "Escape") { e.preventDefault(); this.close(); return; }
      if (["Enter", " "].includes(e.key)) {
        e.preventDefault();
        if (opened === this) this.choose(this.active);
        else this.open();
        return;
      }
      if (["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) {
        e.preventDefault();
        const wasOpen = opened === this;
        if (!wasOpen) this.open();
        let index = this.active, direction = e.key === "ArrowUp" ? -1 : 1;
        if (e.key === "Home") index = 0;
        else if (e.key === "End") { index = this.items.length - 1; direction = -1; }
        else if (wasOpen) index += direction;
        this.highlight(this.available(index, direction));
      } else if (e.key.length === 1) {
        e.preventDefault();
        if (opened !== this) this.open();
        this.prefix = Date.now() - this.typedAt > 700 ? e.key : this.prefix + e.key;
        this.typedAt = Date.now();
        const index = [...this.select.options].findIndex(o => !o.disabled && o.text.toLowerCase().startsWith(this.prefix.toLowerCase()));
        if (index !== -1) this.highlight(index);
      }
    }
  }

  document.addEventListener("pointerdown", e => {
    if (opened && !opened.root.contains(e.target) && !opened.popup.contains(e.target)) opened.close();
  });
  document.addEventListener("focusin", e => {
    if (opened && !opened.root.contains(e.target)) opened.close();
  });
  document.addEventListener("scroll", e => {
    if (!opened || opened.popup.contains(e.target)) return;
    const rect = opened.trigger.getBoundingClientRect();
    if (rect.bottom <= 0 || rect.top >= window.innerHeight) opened.close();
    else opened.position();
  }, true);
  window.addEventListener("resize", () => opened?.close());
  return {
    init() { document.querySelectorAll("select").forEach(s => controls.set(s.id, new Choice(s))); },
    sync() { controls.forEach(c => c.sync()); },
    isOpen() { return opened !== null; },
  };
})();
