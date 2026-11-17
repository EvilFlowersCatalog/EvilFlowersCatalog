/*
 * IP-035: record page tabs. The fieldsets and inlines of a change form become tab panels; one form, so
 * every panel is submitted whatever tab is shown. A tab whose panel holds a validation error is marked and
 * opened first; otherwise the last tab used on this record (sessionStorage) or the URL hash (#tab=…).
 */
(function () {
  "use strict";

  function storageKey() {
    return "console:tab:" + location.pathname;
  }

  function remember(name) {
    try {
      sessionStorage.setItem(storageKey(), name);
    } catch (e) {
      /* private mode: no memory, still works */
    }
  }

  function recall() {
    try {
      return sessionStorage.getItem(storageKey());
    } catch (e) {
      return null;
    }
  }

  function init() {
    var nav = document.querySelector("[data-record-tabs]");
    if (!nav) return;
    var tabs = Array.prototype.slice.call(nav.querySelectorAll("[role=tab]"));
    var panels = {};
    document.querySelectorAll("[data-panel]").forEach(function (panel) {
      panels[panel.getAttribute("data-panel")] = panel;
      // Collapsible fieldsets are already tucked away behind their tab.
      panel.querySelectorAll("details").forEach(function (details) {
        details.open = true;
      });
    });
    if (tabs.length < 2) {
      return; // one section: plain form, no tab bar
    }
    nav.hidden = false;
    document.body.classList.add("record-tabbed");

    function show(name, focus) {
      if (!panels[name]) return;
      tabs.forEach(function (tab) {
        var active = tab.getAttribute("data-tab") === name;
        tab.setAttribute("aria-selected", active ? "true" : "false");
        tab.tabIndex = active ? 0 : -1;
        if (active && focus) tab.focus();
      });
      Object.keys(panels).forEach(function (key) {
        panels[key].hidden = key !== name;
      });
      remember(name);
    }

    var firstError = null;
    tabs.forEach(function (tab) {
      var name = tab.getAttribute("data-tab");
      var panel = panels[name];
      if (panel && panel.querySelector(".errorlist, .errors")) {
        tab.classList.add("has-error");
        if (!firstError) firstError = name;
      }
      tab.addEventListener("click", function () {
        show(name, false);
      });
      tab.addEventListener("keydown", function (event) {
        var index = tabs.indexOf(tab);
        var next = null;
        if (event.key === "ArrowRight") next = tabs[(index + 1) % tabs.length];
        if (event.key === "ArrowLeft") next = tabs[(index - 1 + tabs.length) % tabs.length];
        if (event.key === "Home") next = tabs[0];
        if (event.key === "End") next = tabs[tabs.length - 1];
        if (next) {
          event.preventDefault();
          show(next.getAttribute("data-tab"), true);
        }
      });
    });

    var fromHash = (location.hash.match(/tab=([\w-]+)/) || [])[1];
    var start = firstError || fromHash || recall();
    show(panels[start] ? start : tabs[0].getAttribute("data-tab"), false);
  }

  function copyIds() {
    document.querySelectorAll("[data-copy]").forEach(function (element) {
      element.addEventListener("click", function () {
        if (!navigator.clipboard) return;
        navigator.clipboard.writeText(element.getAttribute("data-copy")).then(function () {
          element.classList.add("copied");
          setTimeout(function () {
            element.classList.remove("copied");
          }, 1200);
        });
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    init();
    copyIds();
  });
})();
