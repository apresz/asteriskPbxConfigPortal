(function () {
  var selectedClass = "ant-menu-item-selected";

  function normalizeArea(area) {
    return area === "dashboard" ? "home" : area || "";
  }

  function setActiveArea(area) {
    var activeArea = normalizeArea(area);
    if (!activeArea) {
      return;
    }

    document.querySelectorAll(".nav__link[data-area]").forEach(function (link) {
      var isActive = link.getAttribute("data-area") === activeArea;
      link.classList.toggle(selectedClass, isActive);
      if (isActive) {
        link.setAttribute("aria-current", "page");
      } else {
        link.removeAttribute("aria-current");
      }
    });
  }

  function syncActiveAreaFromMain() {
    var marker = document.querySelector("#portal-main [data-area]");
    if (marker) {
      setActiveArea(marker.getAttribute("data-area"));
    }
  }

  function formsetContainers(root) {
    var scope = root || document;
    var containers = [];
    if (scope.matches && scope.matches("[data-formset-prefix]")) {
      containers.push(scope);
    }
    scope.querySelectorAll("[data-formset-prefix]").forEach(function (container) {
      containers.push(container);
    });
    return containers;
  }

  function closeFormsetConfirmation(container) {
    container.querySelectorAll(".inline-confirm-popover").forEach(function (popover) {
      popover.remove();
    });
  }

  function removeFormsetRow(row) {
    var idInput = row.querySelector('input[type="hidden"][name$="-id"]');
    var deleteInput = row.querySelector('input[name$="-DELETE"]');

    if (deleteInput && idInput && idInput.value) {
      if (deleteInput.type === "checkbox") {
        deleteInput.checked = true;
      } else {
        deleteInput.value = "on";
      }
      row.hidden = true;
      row.setAttribute("aria-hidden", "true");
      return;
    }

    row.remove();
  }

  function showFormsetDeleteConfirmation(container, row) {
    closeFormsetConfirmation(container);

    var popover = document.createElement("div");
    popover.className = "inline-confirm-popover";
    popover.setAttribute("role", "dialog");
    popover.setAttribute("aria-label", "Delete menu option");
    popover.innerHTML = [
      '<span class="inline-confirm-popover__message">Delete this option?</span>',
      '<span class="inline-confirm-popover__actions">',
      '<button class="button button--secondary ant-btn ant-btn-default" type="button" data-formset-delete-cancel>Cancel</button>',
      '<button class="button button--danger ant-btn ant-btn-dangerous" type="button" data-formset-delete-confirm>Delete</button>',
      "</span>",
    ].join("");

    row.appendChild(popover);

    var confirmButton = popover.querySelector("[data-formset-delete-confirm]");
    if (confirmButton) {
      confirmButton.focus();
    }
  }

  function initDynamicFormsets(root) {
    formsetContainers(root).forEach(function (container) {
      if (container.dataset.formsetReady === "true") {
        return;
      }

      var prefix = container.dataset.formsetPrefix;
      var fieldset = container.closest("fieldset");
      var template = fieldset && fieldset.querySelector("template[data-formset-template]");
      var addButton = fieldset && fieldset.querySelector("[data-formset-add]");
      var totalForms = document.getElementById("id_" + prefix + "-TOTAL_FORMS");
      var maxForms = document.getElementById("id_" + prefix + "-MAX_NUM_FORMS");

      if (!prefix || !template || !addButton || !totalForms) {
        return;
      }

      container.dataset.formsetReady = "true";
      container.addEventListener("click", function (event) {
        var target = event.target && event.target.closest ? event.target : null;
        if (!target) {
          return;
        }

        var deleteButton = target.closest("[data-formset-delete]");
        var cancelButton = target.closest("[data-formset-delete-cancel]");
        var confirmButton = target.closest("[data-formset-delete-confirm]");

        if (deleteButton) {
          var deleteRow = deleteButton.closest(".inline-form-row");
          if (deleteRow) {
            showFormsetDeleteConfirmation(container, deleteRow);
          }
          return;
        }

        if (cancelButton) {
          var cancelRow = cancelButton.closest(".inline-form-row");
          closeFormsetConfirmation(container);
          var rowDeleteButton = cancelRow && cancelRow.querySelector("[data-formset-delete]");
          if (rowDeleteButton) {
            rowDeleteButton.focus();
          }
          return;
        }

        if (confirmButton) {
          var confirmRow = confirmButton.closest(".inline-form-row");
          if (confirmRow) {
            removeFormsetRow(confirmRow);
          }
        }
      });
      container.addEventListener("keydown", function (event) {
        if (event.key === "Escape") {
          closeFormsetConfirmation(container);
        }
      });
      addButton.addEventListener("click", function () {
        var index = parseInt(totalForms.value, 10);
        var max = maxForms && maxForms.value ? parseInt(maxForms.value, 10) : null;

        if (Number.isNaN(index)) {
          index = container.querySelectorAll(".inline-form-row").length;
        }
        if (max !== null && !Number.isNaN(max) && index >= max) {
          return;
        }

        var wrapper = document.createElement("div");
        wrapper.innerHTML = template.innerHTML.replace(/__prefix__/g, String(index)).trim();
        var row = wrapper.firstElementChild;
        if (!row) {
          return;
        }

        container.appendChild(row);
        totalForms.value = String(index + 1);

        var focusTarget = row.querySelector("input:not([type='hidden']), select, textarea");
        if (focusTarget) {
          focusTarget.focus();
        }
      });
    });
  }

  function initializePortal(root) {
    syncActiveAreaFromMain();
    initDynamicFormsets(root || document);
  }

  document.addEventListener("click", function (event) {
    var target = event.target && event.target.closest ? event.target : event.target && event.target.parentElement;
    var link = target && target.closest(".nav__link[data-area]");
    if (link) {
      setActiveArea(link.getAttribute("data-area"));
    }
  });

  document.addEventListener("DOMContentLoaded", function () {
    initializePortal(document);
  });
  document.addEventListener("htmx:afterSwap", function (event) {
    initializePortal(event.detail && event.detail.target ? event.detail.target : document);
  });
  document.addEventListener("htmx:historyRestore", function () {
    initializePortal(document);
  });
  document.addEventListener("htmx:responseError", function () {
    initializePortal(document);
  });
})();
