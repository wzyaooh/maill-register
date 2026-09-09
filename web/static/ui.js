/*
 * 控制台 UI 组件库
 *
 * 控制台不再直接使用浏览器原生控件（select / checkbox / radio / file / progress /
 * details / window.confirm）。所有交互控件都由本文件提供的自定义元素实现，样式集中在
 * ui.css，行为、键盘操作与无障碍语义由组件自身保证。
 *
 * 组件对外暴露与原生控件一致的属性（value / checked / disabled / files …）并派发
 * 冒泡的 input / change 事件，因此业务代码可以像使用原生控件一样使用它们。
 */
(() => {
  "use strict";

  const CONTROL_TAGS = ["ui-input", "ui-number", "ui-textarea", "ui-select",
    "ui-checkbox", "ui-switch", "ui-radio-group", "ui-file"];
  const CONTROL_SELECTOR = CONTROL_TAGS.join(",");
  const FOCUSABLE = 'a[href], button:not(:disabled), input:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])';

  let sequence = 0;
  const uid = (prefix) => `${prefix}-${(sequence += 1)}`;

  function el(tag, className, content) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined && content !== null) node.textContent = String(content);
    return node;
  }

  function paragraphs(message) {
    return String(message ?? "").split("\n").filter((line) => line.trim()).map((line) => el("p", "", line));
  }

  /** 把内部原生元素的事件重新从组件宿主派发，业务代码只会看到组件本身。 */
  function retarget(host, control, types) {
    types.forEach((type) => {
      control.addEventListener(type, (event) => {
        event.stopPropagation();
        host.dispatchEvent(new Event(type, { bubbles: true }));
      });
    });
  }

  function emit(host, types) {
    types.forEach((type) => host.dispatchEvent(new Event(type, { bubbles: true })));
  }

  /**
   * 所有输入类组件的基类：负责渲染时机、禁用状态、标签关联与校验信息展示。
   */
  class UIControl extends HTMLElement {
    constructor() {
      super();
      this._rendered = false;
      this._message = null;
    }

    connectedCallback() {
      this._ensure();
      this._syncDisabled();
      this._linkLabel();
    }

    _ensure() {
      if (this._rendered) return;
      this._rendered = true;
      this.build();
      this._forwardDescription();
    }

    /** 子类实现，创建组件内部结构。 */
    build() {}

    /** 子类实现，返回可获得焦点的内部元素。 */
    get controlElement() {
      return null;
    }

    get controlId() {
      if (!this._controlId) this._controlId = this.id ? `${this.id}-control` : uid("ui-control");
      return this._controlId;
    }

    _forwardDescription() {
      const control = this.controlElement;
      if (!control) return;
      ["aria-describedby", "aria-label", "aria-labelledby"].forEach((name) => {
        const value = this.getAttribute(name);
        if (value === null) return;
        control.setAttribute(name, value);
        this.removeAttribute(name);
      });
    }

    /** 把指向组件的 <label for> 改为指向内部可聚焦元素。 */
    _linkLabel() {
      if (!this.id || this._labelLinked) return;
      const root = this.getRootNode();
      const label = root.querySelector?.(`label[for="${CSS.escape(this.id)}"]`);
      if (!label) return;
      label.htmlFor = this.controlId;
      this._labelLinked = true;
    }

    get disabled() {
      return this.hasAttribute("disabled");
    }

    set disabled(value) {
      this.toggleAttribute("disabled", Boolean(value));
      this._syncDisabled();
    }

    _syncDisabled() {
      this._ensure();
      const disabled = this.disabled;
      this.querySelectorAll("button, input, textarea").forEach((node) => { node.disabled = disabled; });
      this.classList.toggle("is-disabled", disabled);
    }

    get required() {
      return this.hasAttribute("required");
    }

    set required(value) {
      this.toggleAttribute("required", Boolean(value));
    }

    focus(options) {
      this._ensure();
      this.controlElement?.focus(options);
    }

    /** 子类可覆盖，返回校验失败原因；返回空字符串表示通过。 */
    validate() {
      return "";
    }

    get validationMessage() {
      return this.validate();
    }

    checkValidity() {
      return this.validate() === "";
    }

    /** 在组件下方展示校验信息，替代浏览器原生的校验气泡。 */
    setValidity(message) {
      const invalid = Boolean(message);
      this.classList.toggle("is-invalid", invalid);
      this.controlElement?.setAttribute("aria-invalid", invalid ? "true" : "false");
      if (!invalid) {
        this._message?.remove();
        this._message = null;
        return;
      }
      if (!this._message) {
        this._message = el("p", "ui-control-error");
        this._message.id = uid("ui-error");
        this._message.setAttribute("role", "alert");
        this.append(this._message);
      }
      this._message.textContent = message;
    }
  }

  /* ------------------------------------------------------------------ 文本输入 */

  class UIInput extends UIControl {
    static observedAttributes = ["type", "placeholder", "value", "maxlength", "autocomplete", "readonly", "required", "disabled"];

    build() {
      this.box = el("div", "ui-input__box");
      this.input = el("input");
      this.input.className = "ui-input__control";
      this.input.id = this.controlId;
      this.input.type = this.getAttribute("type") || "text";
      if (this.hasAttribute("name")) this.input.name = this.getAttribute("name");
      if (this.hasAttribute("placeholder")) this.input.placeholder = this.getAttribute("placeholder");
      if (this.hasAttribute("autocomplete")) this.input.autocomplete = this.getAttribute("autocomplete");
      if (this.hasAttribute("maxlength")) this.input.maxLength = Number(this.getAttribute("maxlength"));
      if (this.hasAttribute("autofocus")) window.setTimeout(() => this.input.focus(), 0);
      this.input.required = this.hasAttribute("required");
      this.input.readOnly = this.hasAttribute("readonly");
      this.input.value = this.getAttribute("value") || "";
      this.box.append(this.input);
      if (this.input.type === "search") {
        this.box.classList.add("ui-input__box--search");
        this.box.prepend(el("span", "ui-input__icon", "⌕"));
        this.clearButton = el("button", "ui-input__clear", "×");
        this.clearButton.type = "button";
        this.clearButton.tabIndex = -1;
        this.clearButton.setAttribute("aria-label", "清空搜索内容");
        this.clearButton.hidden = !this.input.value;
        this.clearButton.addEventListener("click", () => {
          this.input.value = "";
          this.clearButton.hidden = true;
          this.input.focus();
          emit(this, ["input", "change"]);
        });
        this.box.append(this.clearButton);
      }
      this.input.addEventListener("input", () => {
        if (this.clearButton) this.clearButton.hidden = !this.input.value;
        this.setValidity("");
      });
      retarget(this, this.input, ["input", "change"]);
      this.append(this.box);
    }

    attributeChangedCallback(name, previous, value) {
      if (!this._rendered) return;
      if (name === "type") this.input.type = value || "text";
      if (name === "placeholder") this.input.placeholder = value || "";
      if (name === "maxlength") this.input.maxLength = Number(value) || 524288;
      if (name === "autocomplete") this.input.autocomplete = value || "";
      if (name === "readonly") this.input.readOnly = value !== null;
      if (name === "required") this.input.required = value !== null;
      if (name === "disabled") this._syncDisabled();
    }

    get controlElement() {
      return this.input;
    }

    get value() {
      this._ensure();
      return this.input.value;
    }

    set value(next) {
      this._ensure();
      this.input.value = next ?? "";
      if (this.clearButton) this.clearButton.hidden = !this.input.value;
    }

    get type() {
      return this.getAttribute("type") || "text";
    }

    set type(next) {
      this.setAttribute("type", next);
    }

    get placeholder() {
      return this.getAttribute("placeholder") || "";
    }

    set placeholder(next) {
      this.setAttribute("placeholder", next ?? "");
    }

    get maxLength() {
      this._ensure();
      return this.input.maxLength;
    }

    set maxLength(next) {
      this.setAttribute("maxlength", String(next));
    }

    get autocomplete() {
      return this.getAttribute("autocomplete") || "";
    }

    set autocomplete(next) {
      this.setAttribute("autocomplete", next);
    }

    validate() {
      if (this.disabled || !this.required) return "";
      return this.value.trim() ? "" : "请填写此项。";
    }
  }

  /* ------------------------------------------------------------ 数字输入（步进） */

  class UINumber extends UIControl {
    static observedAttributes = ["min", "max", "step", "value", "placeholder", "required", "disabled"];

    build() {
      this.box = el("div", "ui-number__box");
      this.decrease = el("button", "ui-number__step", "−");
      this.decrease.type = "button";
      this.decrease.tabIndex = -1;
      this.decrease.setAttribute("aria-label", "减少");
      this.increase = el("button", "ui-number__step", "＋");
      this.increase.type = "button";
      this.increase.tabIndex = -1;
      this.increase.setAttribute("aria-label", "增加");
      this.input = el("input");
      this.input.className = "ui-number__control";
      this.input.id = this.controlId;
      this.input.type = "text";
      this.input.inputMode = "numeric";
      this.input.autocomplete = "off";
      this.input.spellcheck = false;
      if (this.hasAttribute("name")) this.input.name = this.getAttribute("name");
      if (this.hasAttribute("placeholder")) this.input.placeholder = this.getAttribute("placeholder");
      this.input.value = this.getAttribute("value") || "";
      this.input.setAttribute("role", "spinbutton");
      this.box.append(this.decrease, this.input, this.increase);
      this.append(this.box);
      this.decrease.addEventListener("click", () => this._nudge(-1));
      this.increase.addEventListener("click", () => this._nudge(1));
      this.input.addEventListener("keydown", (event) => {
        if (event.key === "ArrowUp") { event.preventDefault(); this._nudge(1); }
        if (event.key === "ArrowDown") { event.preventDefault(); this._nudge(-1); }
      });
      this.input.addEventListener("input", () => {
        this.setValidity("");
        this._syncSpin();
      });
      retarget(this, this.input, ["input", "change"]);
      this._syncSpin();
    }

    attributeChangedCallback(name, previous, value) {
      if (!this._rendered) return;
      if (name === "placeholder") this.input.placeholder = value || "";
      if (name === "disabled") this._syncDisabled();
      this._syncSpin();
    }

    get controlElement() {
      return this.input;
    }

    get _bounds() {
      const min = this.hasAttribute("min") ? Number(this.getAttribute("min")) : null;
      const max = this.hasAttribute("max") ? Number(this.getAttribute("max")) : null;
      const step = Number(this.getAttribute("step")) || 1;
      return { min, max, step };
    }

    _nudge(direction) {
      if (this.disabled) return;
      const { min, max, step } = this._bounds;
      const current = Number(this.input.value);
      const base = Number.isFinite(current) && this.input.value.trim() !== "" ? current : min ?? 0;
      let next = base + direction * step;
      if (min !== null) next = Math.max(min, next);
      if (max !== null) next = Math.min(max, next);
      this.input.value = String(next);
      this.setValidity("");
      this._syncSpin();
      emit(this, ["input", "change"]);
    }

    _syncSpin() {
      const { min, max } = this._bounds;
      const value = this.input.value.trim();
      if (min !== null) this.input.setAttribute("aria-valuemin", String(min));
      if (max !== null) this.input.setAttribute("aria-valuemax", String(max));
      if (value !== "") this.input.setAttribute("aria-valuenow", value);
      else this.input.removeAttribute("aria-valuenow");
      if (this.disabled) return;
      this.decrease.disabled = min !== null && Number(value) <= min && value !== "";
      this.increase.disabled = max !== null && Number(value) >= max && value !== "";
    }

    _syncDisabled() {
      super._syncDisabled();
      if (this._rendered && !this.disabled) this._syncSpin();
    }

    get value() {
      this._ensure();
      return this.input.value;
    }

    set value(next) {
      this._ensure();
      this.input.value = next ?? "";
      this._syncSpin();
    }

    get valueAsNumber() {
      return Number(this.value);
    }

    set min(next) { this.setAttribute("min", String(next)); }

    set max(next) { this.setAttribute("max", String(next)); }

    set step(next) { this.setAttribute("step", String(next)); }

    get placeholder() {
      return this.getAttribute("placeholder") || "";
    }

    set placeholder(next) {
      this.setAttribute("placeholder", next ?? "");
    }

    validate() {
      if (this.disabled) return "";
      const value = this.value.trim();
      if (!value) return this.required ? "请填写此项。" : "";
      if (!/^-?\d+$/.test(value)) return "请填写整数。";
      const { min, max } = this._bounds;
      if (min !== null && Number(value) < min) return `不能小于 ${min}。`;
      if (max !== null && Number(value) > max) return `不能大于 ${max}。`;
      return "";
    }
  }

  /* --------------------------------------------------------------- 多行文本域 */

  class UITextarea extends UIControl {
    static observedAttributes = ["rows", "placeholder", "disabled"];

    build() {
      this.box = el("div", "ui-textarea__box");
      this.textarea = el("textarea");
      this.textarea.className = "ui-textarea__control";
      this.textarea.id = this.controlId;
      this.textarea.rows = Number(this.getAttribute("rows")) || 8;
      this.textarea.spellcheck = this.getAttribute("spellcheck") !== "false";
      if (this.hasAttribute("name")) this.textarea.name = this.getAttribute("name");
      if (this.hasAttribute("placeholder")) this.textarea.placeholder = this.getAttribute("placeholder");
      if (this.hasAttribute("autocapitalize")) this.textarea.autocapitalize = this.getAttribute("autocapitalize");
      if (this.hasAttribute("autocomplete")) this.textarea.autocomplete = this.getAttribute("autocomplete");
      this.box.append(this.textarea);
      retarget(this, this.textarea, ["input", "change"]);
      this.append(this.box);
    }

    attributeChangedCallback(name, previous, value) {
      if (!this._rendered) return;
      if (name === "rows") this.textarea.rows = Number(value) || 8;
      if (name === "placeholder") this.textarea.placeholder = value || "";
      if (name === "disabled") this._syncDisabled();
    }

    get controlElement() {
      return this.textarea;
    }

    get value() {
      this._ensure();
      return this.textarea.value;
    }

    set value(next) {
      this._ensure();
      this.textarea.value = next ?? "";
    }
  }

  /* -------------------------------------------------------------------- 下拉选择 */

  class UISelect extends UIControl {
    static observedAttributes = ["placeholder", "disabled"];

    constructor() {
      super();
      this.items = [];
      this._value = "";
      this._activeIndex = -1;
      this._typeahead = "";
      this._typeaheadTimer = 0;
    }

    build() {
      const declared = [...this.querySelectorAll("ui-option")].map((option) => ({
        value: option.getAttribute("value") ?? "",
        label: option.textContent.trim(),
      }));
      const selected = this.querySelector("ui-option[selected]");
      // 声明了占位文案时保持未选中，等待业务代码写入初始值。
      const initial = selected ? selected.getAttribute("value") ?? ""
        : this.hasAttribute("placeholder") ? "" : declared[0]?.value ?? "";
      this.replaceChildren();

      this.trigger = el("button", "ui-select__trigger");
      this.trigger.type = "button";
      this.trigger.id = this.controlId;
      this.trigger.setAttribute("role", "combobox");
      this.trigger.setAttribute("aria-haspopup", "listbox");
      this.trigger.setAttribute("aria-expanded", "false");
      this.valueNode = el("span", "ui-select__value");
      this.trigger.append(this.valueNode, el("span", "ui-select__arrow", "▾"));

      this.menu = el("div", "ui-select__popover");
      this.menu.hidden = true;
      this.listbox = el("ul", "ui-select__listbox");
      this.listbox.id = uid("ui-listbox");
      this.listbox.setAttribute("role", "listbox");
      this.trigger.setAttribute("aria-controls", this.listbox.id);
      this.menu.append(this.listbox);
      this.append(this.trigger, this.menu);

      this.trigger.addEventListener("click", () => this.toggle());
      this.trigger.addEventListener("keydown", (event) => this._onKeydown(event));
      this.listbox.addEventListener("click", (event) => {
        const option = event.target.closest("[data-value]");
        if (!option) return;
        this._commit(option.dataset.value);
        this.close(true);
      });
      this.listbox.addEventListener("mousemove", (event) => {
        const option = event.target.closest("[data-value]");
        if (option) this._setActive(this.items.findIndex((item) => item.value === option.dataset.value), false);
      });
      this._outside = (event) => {
        if (!this.contains(event.target)) this.close(false);
      };

      this.setOptions(declared, initial);
    }

    attributeChangedCallback(name, previous, value) {
      if (!this._rendered) return;
      if (name === "placeholder") this._renderValue();
      if (name === "disabled") {
        this._syncDisabled();
        if (this.disabled) this.close(false);
      }
    }

    get controlElement() {
      return this.trigger;
    }

    /** 重新设置候选项；尽量保留当前选中值。 */
    setOptions(options, fallback = "") {
      this._ensure();
      this.items = options.map((option) => ({ value: String(option.value ?? ""), label: String(option.label ?? option.value ?? "") }));
      const values = this.items.map((item) => item.value);
      const next = values.includes(this._value) ? this._value : values.includes(fallback) ? fallback : "";
      this._value = next;
      this._renderOptions();
      this._renderValue();
    }

    _renderOptions() {
      this.listbox.replaceChildren(...this.items.map((item) => {
        const option = el("li", "ui-select__option", item.label);
        option.dataset.value = item.value;
        option.id = uid("ui-option");
        option.setAttribute("role", "option");
        option.setAttribute("aria-selected", item.value === this._value ? "true" : "false");
        option.classList.toggle("is-selected", item.value === this._value);
        return option;
      }));
    }

    _renderValue() {
      const item = this.items.find((entry) => entry.value === this._value);
      const placeholder = this.getAttribute("placeholder") || "请选择";
      this.valueNode.textContent = item ? item.label : placeholder;
      this.valueNode.classList.toggle("is-placeholder", !item);
    }

    _commit(value) {
      if (value === this._value) return;
      this._value = value;
      this._renderOptions();
      this._renderValue();
      this.setValidity("");
      emit(this, ["input", "change"]);
    }

    get open() {
      return this._rendered && !this.menu.hidden;
    }

    toggle() {
      if (this.open) this.close(true);
      else this.openList();
    }

    openList() {
      if (this.disabled || this.open || !this.items.length) return;
      this.menu.hidden = false;
      this.trigger.setAttribute("aria-expanded", "true");
      this.classList.add("is-open");
      document.addEventListener("pointerdown", this._outside, true);
      const index = this.items.findIndex((item) => item.value === this._value);
      this._setActive(index >= 0 ? index : 0, true);
    }

    close(restoreFocus) {
      if (!this._rendered || this.menu.hidden) return;
      this.menu.hidden = true;
      this.trigger.setAttribute("aria-expanded", "false");
      this.trigger.removeAttribute("aria-activedescendant");
      this.classList.remove("is-open");
      this._activeIndex = -1;
      document.removeEventListener("pointerdown", this._outside, true);
      if (restoreFocus) this.trigger.focus();
    }

    _setActive(index, scroll) {
      if (index < 0 || index >= this.items.length) return;
      this._activeIndex = index;
      const nodes = [...this.listbox.children];
      nodes.forEach((node, position) => node.classList.toggle("is-active", position === index));
      const node = nodes[index];
      if (!node) return;
      this.trigger.setAttribute("aria-activedescendant", node.id);
      if (scroll) node.scrollIntoView({ block: "nearest" });
    }

    _onKeydown(event) {
      const { key } = event;
      if (!this.open) {
        if (["ArrowDown", "ArrowUp", "Enter", " ", "Spacebar"].includes(key)) {
          event.preventDefault();
          this.openList();
        }
        return;
      }
      if (key === "Escape") { event.preventDefault(); this.close(true); return; }
      if (key === "Tab") { this.close(false); return; }
      if (key === "Enter" || key === " " || key === "Spacebar") {
        event.preventDefault();
        const item = this.items[this._activeIndex];
        if (item) this._commit(item.value);
        this.close(true);
        return;
      }
      let next;
      if (key === "ArrowDown") next = Math.min(this.items.length - 1, this._activeIndex + 1);
      if (key === "ArrowUp") next = Math.max(0, this._activeIndex - 1);
      if (key === "Home") next = 0;
      if (key === "End") next = this.items.length - 1;
      if (next !== undefined) {
        event.preventDefault();
        this._setActive(next, true);
        return;
      }
      if (key.length === 1 && !event.metaKey && !event.ctrlKey && !event.altKey) {
        window.clearTimeout(this._typeaheadTimer);
        this._typeahead += key.toLocaleLowerCase();
        this._typeaheadTimer = window.setTimeout(() => { this._typeahead = ""; }, 700);
        const index = this.items.findIndex((item) => item.label.toLocaleLowerCase().startsWith(this._typeahead));
        if (index >= 0) this._setActive(index, true);
      }
    }

    get value() {
      this._ensure();
      return this._value;
    }

    set value(next) {
      this._ensure();
      const value = String(next ?? "");
      if (!this.items.some((item) => item.value === value)) return;
      this._value = value;
      this._renderOptions();
      this._renderValue();
    }

    get placeholder() {
      return this.getAttribute("placeholder") || "";
    }

    set placeholder(next) {
      this.setAttribute("placeholder", next ?? "");
    }

    validate() {
      if (this.disabled || !this.required) return "";
      return this.value ? "" : "请选择一项。";
    }
  }

  /* ------------------------------------------------------------------ 复选与开关 */

  class UIToggle extends UIControl {
    static observedAttributes = ["checked", "disabled", "label", "description", "on-text", "off-text"];

    constructor() {
      super();
      this._checked = false;
      this._indeterminate = false;
    }

    get role() {
      return "checkbox";
    }

    build() {
      this._checked = this.hasAttribute("checked");
      this.control = el("button", `${this.baseClass}__control`);
      this.control.type = "button";
      this.control.id = this.controlId;
      this.control.setAttribute("role", this.role);
      this.control.append(el("span", `${this.baseClass}__mark`));
      this.body = el("span", `${this.baseClass}__body`);
      this.textNode = el("span", `${this.baseClass}__label`, this.getAttribute("label") || "");
      this.body.append(this.textNode);
      if (this.hasAttribute("description")) {
        this.descriptionNode = el("small", "", this.getAttribute("description"));
        this.body.append(this.descriptionNode);
      }
      if (this.hasAttribute("on-text")) {
        this.stateNode = el("span", `${this.baseClass}__state`);
        this.body.append(this.stateNode);
      }
      this.control.addEventListener("click", () => {
        if (this.disabled) return;
        this._indeterminate = false;
        this._checked = !this._checked;
        this._sync();
        emit(this, ["input", "change"]);
      });
      this.append(this.control);
      if (this.getAttribute("label") || this.hasAttribute("description") || this.hasAttribute("on-text")) {
        const label = el("label", `${this.baseClass}__text`);
        label.htmlFor = this.controlId;
        label.append(this.body);
        this.append(label);
      }
      this._sync();
    }

    attributeChangedCallback(name, previous, value) {
      if (!this._rendered) return;
      if (name === "checked") this._checked = value !== null;
      if (name === "label") this.textNode.textContent = value || "";
      if (name === "description" && this.descriptionNode) this.descriptionNode.textContent = value || "";
      if (name === "disabled") this._syncDisabled();
      this._sync();
    }

    get controlElement() {
      return this.control;
    }

    _sync() {
      if (!this._rendered) return;
      this.control.setAttribute("aria-checked", this._indeterminate ? "mixed" : this._checked ? "true" : "false");
      this.classList.toggle("is-checked", this._checked && !this._indeterminate);
      this.classList.toggle("is-indeterminate", this._indeterminate);
      if (this.stateNode) {
        this.stateNode.textContent = this._checked
          ? this.getAttribute("on-text") || "已启用"
          : this.getAttribute("off-text") || "未启用";
      }
    }

    get checked() {
      this._ensure();
      return this._checked;
    }

    set checked(value) {
      this._ensure();
      this._checked = Boolean(value);
      if (this._checked) this._indeterminate = false;
      this._sync();
    }

    get indeterminate() {
      return this._indeterminate;
    }

    set indeterminate(value) {
      this._ensure();
      this._indeterminate = Boolean(value);
      this._sync();
    }

    get label() {
      return this.getAttribute("label") || "";
    }

    set label(next) {
      this.setAttribute("label", next ?? "");
    }
  }

  class UICheckbox extends UIToggle {
    get baseClass() {
      return "ui-checkbox";
    }
  }

  class UISwitch extends UIToggle {
    get baseClass() {
      return "ui-switch";
    }

    get role() {
      return "switch";
    }
  }

  /* ------------------------------------------------------------------ 单选卡片组 */

  class UIRadioGroup extends UIControl {
    static observedAttributes = ["value", "disabled"];

    constructor() {
      super();
      this._value = "";
    }

    build() {
      this.setAttribute("role", "radiogroup");
      this.radios = [...this.querySelectorAll("ui-radio")];
      this.radios.forEach((radio) => radio.attach(this));
      this._value = this.getAttribute("value") || this.radios[0]?.value || "";
      this._sync();
      this.addEventListener("keydown", (event) => this._onKeydown(event));
    }

    get controlElement() {
      return this.radios?.find((radio) => radio.value === this._value)?.control || this.radios?.[0]?.control || null;
    }

    _onKeydown(event) {
      const keys = ["ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp", "Home", "End"];
      if (!keys.includes(event.key) || this.disabled) return;
      event.preventDefault();
      const index = this.radios.findIndex((radio) => radio.value === this._value);
      const total = this.radios.length;
      let next = index;
      if (event.key === "ArrowRight" || event.key === "ArrowDown") next = (index + 1) % total;
      if (event.key === "ArrowLeft" || event.key === "ArrowUp") next = (index - 1 + total) % total;
      if (event.key === "Home") next = 0;
      if (event.key === "End") next = total - 1;
      this.select(this.radios[next]?.value, true);
    }

    select(value, focus) {
      if (value === undefined || value === this._value) return;
      this._value = value;
      this._sync();
      if (focus) this.controlElement?.focus();
      emit(this, ["input", "change"]);
    }

    _sync() {
      this.radios?.forEach((radio) => radio.setChecked(radio.value === this._value));
    }

    _syncDisabled() {
      this._ensure();
      this.classList.toggle("is-disabled", this.disabled);
      this.radios?.forEach((radio) => { radio.control.disabled = this.disabled; });
    }

    get value() {
      this._ensure();
      return this._value;
    }

    set value(next) {
      this._ensure();
      if (!this.radios.some((radio) => radio.value === next)) return;
      this._value = String(next);
      this._sync();
    }
  }

  class UIRadio extends HTMLElement {
    get value() {
      return this.getAttribute("value") || "";
    }

    attach(group) {
      this.group = group;
      this.control = el("button", "ui-radio__card");
      this.control.type = "button";
      this.control.setAttribute("role", "radio");
      this.control.setAttribute("aria-checked", "false");
      const symbol = this.getAttribute("symbol");
      if (symbol) {
        const mark = el("span", "ui-radio__symbol", symbol);
        mark.setAttribute("aria-hidden", "true");
        this.control.append(mark);
      }
      this.control.append(el("strong", "", this.getAttribute("label") || this.value));
      if (this.hasAttribute("summary")) this.control.append(el("span", "", this.getAttribute("summary")));
      if (this.hasAttribute("description")) this.control.append(el("small", "", this.getAttribute("description")));
      this.control.addEventListener("click", () => group.select(this.value, false));
      this.replaceChildren(this.control);
    }

    setChecked(checked) {
      if (!this.control) return;
      this.control.setAttribute("aria-checked", checked ? "true" : "false");
      this.control.tabIndex = checked ? 0 : -1;
      this.classList.toggle("is-checked", checked);
    }
  }

  /* -------------------------------------------------------------------- 文件选择 */

  class UIFile extends UIControl {
    static observedAttributes = ["disabled"];

    build() {
      this.input = el("input");
      this.input.type = "file";
      this.input.className = "ui-file__native";
      this.input.tabIndex = -1;
      this.input.setAttribute("aria-hidden", "true");
      if (this.hasAttribute("accept")) this.input.accept = this.getAttribute("accept");
      this.trigger = el("button", "ui-file__button", this.getAttribute("button-text") || "选择文件");
      this.trigger.type = "button";
      this.trigger.id = this.controlId;
      this.nameNode = el("span", "ui-file__name", this.getAttribute("placeholder") || "未选择文件");
      this.trigger.addEventListener("click", () => {
        if (!this.disabled) this.input.click();
      });
      this.input.addEventListener("change", (event) => {
        event.stopPropagation();
        this.nameNode.textContent = this.input.files[0]?.name || this.getAttribute("placeholder") || "未选择文件";
        this.classList.toggle("has-file", Boolean(this.input.files.length));
        emit(this, ["change"]);
      });
      this.append(this.input, this.trigger, this.nameNode);
    }

    attributeChangedCallback(name) {
      if (this._rendered && name === "disabled") this._syncDisabled();
    }

    _syncDisabled() {
      this._ensure();
      this.classList.toggle("is-disabled", this.disabled);
      this.trigger.disabled = this.disabled;
      this.input.disabled = this.disabled;
    }

    get controlElement() {
      return this.trigger;
    }

    get files() {
      this._ensure();
      return this.input.files;
    }

    get value() {
      this._ensure();
      return this.input.value;
    }

    set value(next) {
      this._ensure();
      this.input.value = next ?? "";
      this.nameNode.textContent = this.getAttribute("placeholder") || "未选择文件";
      this.classList.remove("has-file");
    }
  }

  /* ---------------------------------------------------------------------- 进度条 */

  class UIProgress extends HTMLElement {
    static observedAttributes = ["value", "max"];

    connectedCallback() {
      this._ensure();
    }

    _ensure() {
      if (this._rendered) return;
      this._rendered = true;
      this.setAttribute("role", "progressbar");
      this.fill = el("span", "ui-progress__fill");
      const track = el("span", "ui-progress__track");
      track.append(this.fill);
      this.append(track);
      this._sync();
    }

    attributeChangedCallback() {
      if (this._rendered) this._sync();
    }

    _sync() {
      const max = Math.max(Number(this.getAttribute("max")) || 0, 1);
      const value = Math.min(Math.max(Number(this.getAttribute("value")) || 0, 0), max);
      this.fill.style.width = `${(value / max) * 100}%`;
      this.setAttribute("aria-valuemin", "0");
      this.setAttribute("aria-valuemax", String(max));
      this.setAttribute("aria-valuenow", String(value));
    }

    get value() {
      return Number(this.getAttribute("value")) || 0;
    }

    set value(next) {
      this.setAttribute("value", String(next));
    }

    get max() {
      return Number(this.getAttribute("max")) || 1;
    }

    set max(next) {
      this.setAttribute("max", String(next));
    }
  }

  /* ------------------------------------------------------------------ 折叠展开区 */

  class UIDisclosure extends HTMLElement {
    static observedAttributes = ["open", "label"];

    connectedCallback() {
      this._ensure();
    }

    _ensure() {
      if (this._rendered) return;
      this._rendered = true;
      this.panel = el("div", "ui-disclosure__panel");
      this.panel.id = uid("ui-panel");
      this.panel.append(...this.childNodes);
      this.trigger = el("button", "ui-disclosure__trigger");
      this.trigger.type = "button";
      this.trigger.setAttribute("aria-controls", this.panel.id);
      this.trigger.append(el("span", "ui-disclosure__caret", "▸"), el("span", "", this.getAttribute("label") || "展开"));
      this.trigger.addEventListener("click", () => { this.open = !this.open; });
      this.replaceChildren(this.trigger, this.panel);
      this._sync();
    }

    attributeChangedCallback(name, previous, value) {
      if (!this._rendered) return;
      if (name === "label") this.trigger.lastElementChild.textContent = value || "展开";
      this._sync();
    }

    _sync() {
      const open = this.open;
      this.trigger.setAttribute("aria-expanded", open ? "true" : "false");
      this.panel.hidden = !open;
      this.classList.toggle("is-open", open);
    }

    get open() {
      return this.hasAttribute("open");
    }

    set open(value) {
      this._ensure();
      this.toggleAttribute("open", Boolean(value));
    }
  }

  /* ------------------------------------------------------------------ 模态对话框 */

  let activeDialog = null;

  function dialogRoot() {
    let root = document.getElementById("ui-dialog-root");
    if (!root) {
      root = el("div", "ui-dialog-root");
      root.id = "ui-dialog-root";
      document.body.append(root);
    }
    return root;
  }

  /**
   * 组件化的确认对话框，替代浏览器原生 window.confirm。
   * 返回 Promise<boolean>：确认为 true，取消 / Esc / 点击遮罩为 false。
   */
  function confirmDialog({ title = "请确认操作", message = "", confirmText = "确定", cancelText = "取消", tone = "primary" } = {}) {
    activeDialog?.dismiss();
    const previousFocus = document.activeElement;
    const overlay = el("div", "ui-dialog-overlay");
    const panel = el("div", `ui-dialog ui-dialog--${tone}`);
    panel.setAttribute("role", "alertdialog");
    panel.setAttribute("aria-modal", "true");
    const heading = el("h2", "ui-dialog__title", title);
    heading.id = uid("ui-dialog-title");
    panel.setAttribute("aria-labelledby", heading.id);
    const body = el("div", "ui-dialog__body");
    body.id = uid("ui-dialog-body");
    body.append(...paragraphs(message));
    if (body.childElementCount) panel.setAttribute("aria-describedby", body.id);
    const cancel = el("button", "button secondary", cancelText);
    cancel.type = "button";
    const accept = el("button", `button ${tone === "danger" ? "danger" : "primary"}`, confirmText);
    accept.type = "button";
    const footer = el("div", "ui-dialog__footer");
    footer.append(cancel, accept);
    panel.append(heading, body, footer);
    overlay.append(panel);

    return new Promise((resolve) => {
      let settled = false;
      const finish = (result) => {
        if (settled) return;
        settled = true;
        activeDialog = null;
        document.removeEventListener("keydown", onKeydown, true);
        overlay.remove();
        if (!dialogRoot().childElementCount) document.body.classList.remove("ui-dialog-open");
        if (previousFocus instanceof HTMLElement && previousFocus.isConnected) previousFocus.focus({ preventScroll: true });
        resolve(result);
      };
      const onKeydown = (event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          finish(false);
          return;
        }
        if (event.key !== "Tab") return;
        const focusable = [...panel.querySelectorAll(FOCUSABLE)];
        if (!focusable.length) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
        else if (!panel.contains(document.activeElement)) { event.preventDefault(); first.focus(); }
      };
      overlay.addEventListener("pointerdown", (event) => {
        if (event.target === overlay) finish(false);
      });
      cancel.addEventListener("click", () => finish(false));
      accept.addEventListener("click", () => finish(true));
      document.addEventListener("keydown", onKeydown, true);
      dialogRoot().append(overlay);
      document.body.classList.add("ui-dialog-open");
      activeDialog = { dismiss: () => finish(false) };
      accept.focus({ preventScroll: true });
    });
  }

  /* ------------------------------------------------------------------ 表单校验 */

  function controls(scope) {
    return [...scope.querySelectorAll(CONTROL_SELECTOR)];
  }

  /**
   * 组件化的表单校验，替代 form.reportValidity() 的原生气泡提示。
   * onInvalid 可用于在聚焦前让被隐藏的字段重新可见。
   */
  function validate(form, { onInvalid } = {}) {
    let first = null;
    controls(form).forEach((control) => {
      const message = control.disabled ? "" : control.validate();
      control.setValidity(message);
      if (message && !first) first = control;
    });
    if (!first) return true;
    onInvalid?.(first);
    first.focus();
    return false;
  }

  function clearValidity(form) {
    controls(form).forEach((control) => control.setValidity(""));
  }

  /* --------------------------------------------------------------- 工厂函数 */

  function create(tag, attributes = {}) {
    const node = document.createElement(tag);
    Object.entries(attributes).forEach(([name, value]) => {
      if (value === undefined || value === null || value === false) return;
      node.setAttribute(name, value === true ? "" : String(value));
    });
    return node;
  }

  function disclosure(label, children, { open = false, className = "" } = {}) {
    const node = create("ui-disclosure", { label, open, class: className });
    node.append(...children);
    return node;
  }

  function progress(value, max, ariaLabel) {
    return create("ui-progress", { value, max: Math.max(Number(max) || 0, 1), "aria-label": ariaLabel });
  }

  // 子元素必须先于容器定义，容器升级时才能调用子元素的方法。
  customElements.define("ui-option", class extends HTMLElement {});
  customElements.define("ui-radio", UIRadio);
  customElements.define("ui-input", UIInput);
  customElements.define("ui-number", UINumber);
  customElements.define("ui-textarea", UITextarea);
  customElements.define("ui-select", UISelect);
  customElements.define("ui-checkbox", UICheckbox);
  customElements.define("ui-switch", UISwitch);
  customElements.define("ui-radio-group", UIRadioGroup);
  customElements.define("ui-file", UIFile);
  customElements.define("ui-progress", UIProgress);
  customElements.define("ui-disclosure", UIDisclosure);

  window.UI = { CONTROL_SELECTOR, confirm: confirmDialog, validate, clearValidity, controls, create, disclosure, progress };
})();
