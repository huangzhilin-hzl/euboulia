"use strict";

// Keep the select as the application's value/options model. The visible controls
// use page styles, including the popup, instead of the platform's native menu.
window.PlaygroundChoices = (() => {
  const controls = new Map();
  const statuses = new Set(["idle", "partial", "busy", "unknown", "not_ready", "no_gpu"]);
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
        attributeFilter: ["disabled", "title", "selected", "data-choice-label", "data-choice-status", "data-choice-summary", "data-choice-brief"],
      });
      this.sync();
    }

    renderValue(target, option, compact = false) {
      const rich = option && statuses.has(option.dataset.choiceStatus) && option.dataset.choiceSummary;
      const labelText = rich ? option.dataset.choiceLabel || option.text : option?.text || "Select…";
      const statusText = rich ? compact ? option.dataset.choiceBrief || option.dataset.choiceSummary : option.dataset.choiceSummary : "";
      const key = JSON.stringify([!!rich, labelText, rich ? option.dataset.choiceStatus : "", statusText]);
      if (target.choiceValueKey === key) return;
      target.choiceValueKey = key;
      target.classList.toggle("choice-rich", !!rich);
      if (!rich) {
        if (target.textContent !== labelText) target.textContent = labelText;
        return;
      }
      let label = target.querySelector(".choice-label"), status = target.querySelector(".choice-status");
      if (!label || !status) {
        label = document.createElement("span"); label.className = "choice-label";
        status = document.createElement("span"); status.className = "choice-status";
        target.replaceChildren(label, status);
      }
      if (label.textContent !== labelText) label.textContent = labelText;
      if (status.dataset.status !== option.dataset.choiceStatus) status.dataset.status = option.dataset.choiceStatus;
      if (status.textContent !== statusText) status.textContent = statusText;
    }

    attribute(target, name, value) {
      if (target.getAttribute(name) !== String(value)) target.setAttribute(name, String(value));
    }

    chooseItem(index) {
      const value = this.items[index]?.dataset.value;
      this.choose([...this.select.options].findIndex(option => option.value === value));
    }

    createItem(option) {
      const item = document.createElement(this.segmented ? "button" : "div");
      item.className = "choice-item";
      item.dataset.value = option.value;
      item.setAttribute("role", this.segmented ? "radio" : "option");
      item.onclick = () => this.chooseItem(this.items.indexOf(item));
      if (this.segmented) {
        item.type = "button";
        item.onkeydown = e => {
          const index = this.items.indexOf(item);
          let next = index;
          if (e.key === "ArrowRight" || e.key === "ArrowDown") next++;
          else if (e.key === "ArrowLeft" || e.key === "ArrowUp") next--;
          else if (e.key === "Home") next = 0;
          else if (e.key === "End") next = this.items.length - 1;
          else return;
          e.preventDefault();
          const target = this.available(next, next < index ? -1 : 1);
          this.chooseItem(target);
          this.items[target]?.focus();
        };
      } else item.onpointermove = () => this.highlight(this.items.indexOf(item), false);
      return item;
    }

    sync() {
      const options = [...this.select.options];
      const key = JSON.stringify(options.map(o => [o.value, o.text, o.disabled, o.title,
        o.dataset.choiceLabel, o.dataset.choiceStatus, o.dataset.choiceSummary, o.dataset.choiceBrief]));
      if (key !== this.key) {
        const focusedValue = this.root.querySelector(":focus")?.dataset.value;
        const activeValue = opened === this ? this.items?.[this.active]?.dataset.value : undefined;
        const scrollTop = this.popup?.scrollTop;
        this.key = key;
        const container = this.segmented ? this.root : this.popup;
        const existing = new Map((this.items || []).map(item => [item.dataset.value, item]));
        // Keep the hovered/keyboard node in place until its menu closes.
        let ordered = options;
        if (opened === this && this.select.id === "node") {
          const remaining = new Map(options.map(option => [option.value, option]));
          ordered = (this.items || []).flatMap(item => {
            const option = remaining.get(item.dataset.value);
            remaining.delete(item.dataset.value);
            return option ? [option] : [];
          }).concat([...remaining.values()]);
        }
        const items = ordered.map(option => {
          const item = existing.get(option.value) || this.createItem(option);
          existing.delete(option.value);
          const id = this.select.id + "-option-" + options.indexOf(option);
          if (item.id !== id) item.id = id;
          if (item.title !== (option.title || option.text)) item.title = option.title || option.text;
          this.renderValue(item, option);
          return item;
        });
        existing.forEach(item => item.remove());
        let previous = this.segmented ? this.select : null;
        for (const item of items) {
          const next = previous ? previous.nextElementSibling : container.firstElementChild;
          if (next !== item) container.insertBefore(item, next);
          previous = item;
        }
        this.items = items;
        if (this.popup && this.popup.scrollTop !== scrollTop) this.popup.scrollTop = scrollTop;
        if (focusedValue !== undefined) this.items.find(i => i.dataset.value === focusedValue)?.focus();
        if (activeValue !== undefined) {
          const active = this.items.findIndex(item => item.dataset.value === activeValue);
          if (active !== -1) this.active = active;
        }
      }
      const selected = this.select.selectedIndex;
      this.items.forEach(item => {
        const option = options.find(option => option.value === item.dataset.value);
        const disabled = this.select.disabled || option.disabled;
        const checked = item.dataset.value === this.select.value;
        this.attribute(item, this.segmented ? "aria-checked" : "aria-selected", checked);
        this.attribute(item, "aria-disabled", disabled);
        if (this.segmented) {
          if (item.disabled !== disabled) item.disabled = disabled;
          const tabIndex = checked ? 0 : -1;
          if (item.tabIndex !== tabIndex) item.tabIndex = tabIndex;
        }
      });
      if (!this.segmented) {
        this.renderValue(this.text, options[selected], true);
        this.root.classList.toggle("choice-with-status", this.text.classList.contains("choice-rich"));
        this.attribute(this.trigger, "aria-label", this.text.classList.contains("choice-rich")
          ? `${this.label}: ${options[selected].text}` : this.label);
        const describedBy = this.select.getAttribute("aria-describedby");
        if (describedBy) this.attribute(this.trigger, "aria-describedby", describedBy);
        const disabled = this.select.disabled || !options.length;
        if (this.trigger.disabled !== disabled) this.trigger.disabled = disabled;
        const title = this.select.title || options[selected]?.title || options[selected]?.text || this.label;
        if (this.trigger.title !== title) this.trigger.title = title;
        if (this.trigger.disabled) this.close();
        else if (opened === this) {
          this.highlight(this.available(Math.max(0, Math.min(this.active, this.items.length - 1)), 1), false);
          this.position();
        }
      }
    }

    available(index, direction) {
      const length = this.items.length;
      for (let n = 0; n < length; n++, index += direction) {
        const candidate = (index % length + length) % length;
        const option = [...this.select.options].find(option => option.value === this.items[candidate].dataset.value);
        if (option && !option.disabled) return candidate;
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
      this.select.dispatchEvent(new CustomEvent("choice-select", {bubbles: true, detail: {value}}));
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
      this.highlight(this.available(Math.max(0, this.items.findIndex(item => item.dataset.value === this.select.value)), 1));
    }

    close() {
      if (opened !== this) return;
      opened = null;
      this.popup.hidden = true;
      this.trigger.setAttribute("aria-expanded", "false");
      this.trigger.removeAttribute("aria-activedescendant");
      this.prefix = "";
      if (this.select.id === "node") {
        this.key = "";
        this.sync();
      }
    }

    highlight(index, scroll = true) {
      if (index === -1) {
        this.active = -1;
        this.items.forEach(item => item.classList.toggle("active", false));
        this.trigger.removeAttribute("aria-activedescendant");
        return;
      }
      const item = this.items[index];
      const option = item && [...this.select.options].find(option => option.value === item.dataset.value);
      if (!option || option.disabled) return;
      this.active = index;
      this.items.forEach((item, i) => item.classList.toggle("active", i === index));
      this.attribute(this.trigger, "aria-activedescendant", this.items[index].id);
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
        if (opened === this) this.chooseItem(this.active);
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
        const index = this.items.findIndex(item => {
          const option = [...this.select.options].find(option => option.value === item.dataset.value);
          return option && !option.disabled && option.text.toLowerCase().startsWith(this.prefix.toLowerCase());
        });
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
