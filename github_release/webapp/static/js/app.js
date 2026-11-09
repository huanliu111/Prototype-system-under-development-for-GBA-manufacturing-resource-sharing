/* MIoT — small, dependency-free interactions. */
(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  /* ---------------------------------------------------------- sidebar */
  const shell = $(".app-shell");
  const openSidebar = () => shell && shell.classList.add("sidebar-open");
  const closeSidebar = () => shell && shell.classList.remove("sidebar-open");
  $$("[data-sidebar-toggle]").forEach((button) => button.addEventListener("click", openSidebar));
  $$("[data-sidebar-close]").forEach((button) => button.addEventListener("click", closeSidebar));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeSidebar();
  });

  /* ------------------------------------------------------------ flashes */
  const closeFlash = (flash) => {
    flash.style.opacity = "0";
    flash.style.transform = "translateY(-4px)";
    window.setTimeout(() => flash.remove(), 180);
  };
  $$("[data-flash-close]").forEach((button) => {
    button.addEventListener("click", () => closeFlash(button.closest("[data-flash]")));
  });
  $$("[data-flash]").forEach((flash) => {
    window.setTimeout(() => {
      if (document.body.contains(flash)) closeFlash(flash);
    }, 7000);
  });

  /* -------------------------------------------------------- password UI */
  $$("[data-password-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      const input = $("#login-password") || $("input[type=password]", button.closest("form"));
      if (!input) return;
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      button.textContent = show
        ? (button.dataset.hideLabel || "隐藏")
        : (button.dataset.showLabel || "显示");
    });
  });

  $$("[data-demo-fill]").forEach((button) => {
    button.addEventListener("click", () => {
      const form = $("[data-login-form]");
      if (!form) return;
      const username = form.querySelector("input[name=username]");
      const password = form.querySelector("input[name=password]");
      username.value = button.dataset.demoFill || "";
      password.value = button.dataset.demoPassword || "";
      if (button.hasAttribute('data-demo-login')) {
        form.requestSubmit();
        return;
      }
      password.focus();
      form.scrollIntoView({ behavior: "smooth", block: "center" });
    });
  });

  /* ------------------------------------------------------ dynamic rows */
  const rowMarkup = (role) => {
    const isBuyer = role === "buyer";
    const prefix = isBuyer ? "buyer" : "seller";
    const nameLabel = isBuyer ? "需求企业名称" : "供给企业名称";
    const valuesLabel = isBuyer ? "逐单位估值" : "逐单位成本";
    const placeholder = isBuyer ? "例如：360, 340, 320" : "例如：120, 140, 160";
    return `
      <span class="row-index" data-row-index>${isBuyer ? "B" : "S"}01</span>
      <label class="sr-only" for="${prefix}-name-new">${nameLabel}</label>
      <input class="input" id="${prefix}-name-new" name="${prefix}_name" placeholder="企业名称" maxlength="40">
      <label class="sr-only" for="${prefix}-values-new">${valuesLabel}</label>
      <input class="input mono" id="${prefix}-values-new" name="${prefix}_values" placeholder="${placeholder}" data-values-input>
      <button class="icon-btn row-remove" type="button" data-remove-row aria-label="删除该行">
        <svg class="icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
             stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="m19 6-1 15H6L5 6"/><path d="M10 11v6M14 11v6"/>
        </svg>
      </button>`;
  };

  function updateRows(role) {
    const list = $(`[data-${role}-rows]`);
    if (!list) return;
    const prefix = role === "buyer" ? "B" : "S";
    const namePrefix = role;
    $$(".enterprise-row", list).forEach((row, index) => {
      const number = String(index + 1).padStart(2, "0");
      const badge = $("[data-row-index]", row);
      if (badge) badge.textContent = `${prefix}${number}`;
      const nameInput = row.querySelector(`input[name=${namePrefix}_name]`);
      const valuesInput = row.querySelector(`input[name=${namePrefix}_values]`);
      if (nameInput) {
        nameInput.id = `${namePrefix}-name-${index + 1}`;
        const label = row.querySelector(`label[for^="${namePrefix}-name"]`);
        if (label) label.htmlFor = nameInput.id;
      }
      if (valuesInput) {
        valuesInput.id = `${namePrefix}-values-${index + 1}`;
        const label = row.querySelector(`label[for^="${namePrefix}-values"]`);
        if (label) label.htmlFor = valuesInput.id;
      }
    });
    const counter = $(`[data-${role}-count]`);
    if (counter) counter.textContent = String($$(".enterprise-row", list).length);
  }

  function addRow(role) {
    const list = $(`[data-${role}-rows]`);
    if (!list) return;
    if ($$(".enterprise-row", list).length >= 30) {
      window.alert("最多支持 30 家企业。");
      return;
    }
    const row = document.createElement("div");
    row.className = "enterprise-row";
    row.setAttribute("data-enterprise-row", "");
    row.innerHTML = rowMarkup(role);
    list.appendChild(row);
    updateRows(role);
    updateSummary();
    const input = row.querySelector("input");
    if (input) input.focus();
  }

  $$("[data-add-buyer]").forEach((button) => button.addEventListener("click", () => addRow("buyer")));
  $$("[data-add-seller]").forEach((button) => button.addEventListener("click", () => addRow("seller")));

  document.addEventListener("click", (event) => {
    const removeButton = event.target.closest("[data-remove-row]");
    if (!removeButton) return;
    const row = removeButton.closest(".enterprise-row");
    const list = row && row.parentElement;
    if (!row || !list) return;
    if ($$(".enterprise-row", list).length <= 1) {
      window.alert("至少保留一家企业。");
      return;
    }
    row.remove();
    updateRows(list.hasAttribute("data-buyer-rows") ? "buyer" : "seller");
    updateSummary();
  });

  /* --------------------------------------------------- live form summary */
  const form = $("#experiment-form");
  const parseNumbers = (value) => String(value || "")
    .replace(/，/g, " ")
    .split(/[\s,;；]+/)
    .map((part) => part.trim())
    .filter((part) => part.length > 0);

  function currentRows(role) {
    const list = $(`[data-${role}-rows]`);
    return list ? $$(".enterprise-row", list) : [];
  }

  function updateSummary() {
    if (!form) return;
    const buyers = currentRows("buyer");
    const sellers = currentRows("seller");
    const buyerValues = buyers.map((row) => row.querySelector("input[name=buyer_values]")?.value || "");
    const sellerValues = sellers.map((row) => row.querySelector("input[name=seller_values]")?.value || "");
    const buyerUnitCounts = buyerValues.map((value) => parseNumbers(value).length).filter(Boolean);
    const sellerUnitCounts = sellerValues.map((value) => parseNumbers(value).length).filter(Boolean);
    const unitCounts = buyerUnitCounts.concat(sellerUnitCounts);
    const unique = Array.from(new Set(unitCounts));
    const unitLabel = unique.length === 0 ? "—" : (unique.length === 1 ? String(unique[0]) : "不一致");

    const setText = (selector, value) => {
      const target = $(selector);
      if (target) target.textContent = value;
    };
    setText("[data-summary-buyers]", String(buyers.length));
    setText("[data-summary-sellers]", String(sellers.length));
    setText("[data-summary-traders]", String(buyers.length + sellers.length));
    setText("[data-summary-units]", unitLabel);
    setText("[data-buyer-count]", String(buyers.length));
    setText("[data-seller-count]", String(sellers.length));
    setText("[data-summary-periods]", `${form.periods?.value || "—"} 个`);
    setText("[data-summary-rounds]", form.rounds?.value || "—");
    setText("[data-summary-price]", `${form.price_floor?.value || "—"} – ${form.price_ceiling?.value || "—"}`);
    setText("[data-summary-seed]", form.seed?.value || "—");

    buyers.forEach((row, index) => {
      const badge = $("[data-row-index]", row);
      if (badge) badge.textContent = `B${String(index + 1).padStart(2, "0")}`;
    });
    sellers.forEach((row, index) => {
      const badge = $("[data-row-index]", row);
      if (badge) badge.textContent = `S${String(index + 1).padStart(2, "0")}`;
    });
  }

  if (form) {
    form.addEventListener("input", updateSummary);
    form.addEventListener("change", updateSummary);
    updateSummary();
  }

  $$("[data-random-seed]").forEach((button) => {
    button.addEventListener("click", () => {
      const seed = form?.seed;
      if (seed) {
        seed.value = String(Math.floor(Math.random() * 900000) + 100000);
        updateSummary();
      }
    });
  });

  const clientError = (message) => {
    let box = $("#form-client-error");
    if (!box) {
      box = document.createElement("div");
      box.id = "form-client-error";
      box.className = "alert alert-error";
      box.setAttribute("role", "alert");
      const main = $(".form-main");
      if (main) main.prepend(box);
    }
    box.textContent = message;
    box.scrollIntoView({ behavior: "smooth", block: "center" });
  };

  const clearClientError = () => {
    const box = $("#form-client-error");
    if (box) box.remove();
  };

  if (form) {
    form.addEventListener("submit", (event) => {
      clearClientError();
      const buyers = currentRows("buyer");
      const sellers = currentRows("seller");
      const errors = [];
      if (!buyers.length || !sellers.length) errors.push("至少需要一家需求企业和一家供给企业。");

      const unitSets = [];
      const checkRows = (rows, role) => {
        rows.forEach((row, index) => {
          const name = row.querySelector(`input[name=${role}_name]`)?.value.trim() || "";
          const values = parseNumbers(row.querySelector(`input[name=${role}_values]`)?.value || "");
          if (!name && !values.length) return;
          if (!name) errors.push(`${role === "buyer" ? "需求" : "供给"}企业第 ${index + 1} 行缺少名称。`);
          if (!values.length) errors.push(`“${name || "未命名企业"}”还没有填写逐单位数值。`);
          else {
            if (values.some((value) => !/^\d+$/.test(value))) {
              errors.push(`“${name || "未命名企业"}”的数值必须是整数，并用逗号分隔。`);
            }
            unitSets.push(values.length);
          }
        });
      };
      checkRows(buyers, "buyer");
      checkRows(sellers, "seller");

      const uniqueUnits = Array.from(new Set(unitSets));
      if (uniqueUnits.length > 1) errors.push("每家企业的逐单位估值/成本数量必须一致。");
      if (form.price_ceiling && form.price_floor && Number(form.price_ceiling.value) <= Number(form.price_floor.value)) {
        errors.push("价格上限必须大于价格下限。");
      }

      if (errors.length) {
        event.preventDefault();
        clientError(errors[0]);
        return;
      }

      const overlay = $("[data-run-overlay]");
      const submit = $("[data-submit-experiment]");
      if (overlay) overlay.hidden = false;
      if (submit) {
        submit.disabled = true;
        submit.dataset.originalText = submit.innerHTML;
        submit.innerHTML = '<span class="run-spinner" style="width:17px;height:17px;border-width:2px;"></span> 正在计算…';
      }
    });
  }

  /* --------------------------------------------------------- confirmations */
  $$("form[data-confirm]").forEach((confirmForm) => {
    confirmForm.addEventListener("submit", (event) => {
      if (!window.confirm(confirmForm.dataset.confirm || "确定执行此操作吗？")) {
        event.preventDefault();
      }
    });
  });
})();
