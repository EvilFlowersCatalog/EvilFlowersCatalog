/*
 * IP-035: small helpers every console page shares. No build step, no modules: one global `Console`.
 */
(function () {
  "use strict";

  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta && meta.content) return meta.content;
    var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  /** fetch() a console JSON endpoint; resolves with the parsed body, rejects with an Error carrying `.status`. */
  function api(url, options) {
    options = options || {};
    var init = {
      method: options.method || "GET",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      signal: options.signal,
    };
    if (options.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }
    if (init.method !== "GET") init.headers["X-CSRFToken"] = csrfToken();
    return fetch(url, init).then(function (response) {
      return response
        .json()
        .catch(function () {
          return {};
        })
        .then(function (data) {
          if (!response.ok) {
            var error = new Error((data && data.error) || response.status + " " + response.statusText);
            error.status = response.status;
            throw error;
          }
          return data;
        });
    });
  }

  /** The value of a CSS custom property (`Console.css('--e-accent')`), resolved for the current theme. */
  function css(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  /** The categorical palette (`--e-series-1..8`) for charts and graph nodes. */
  function series() {
    var out = [];
    for (var i = 1; i <= 8; i++) out.push(css("--e-series-" + i));
    return out;
  }

  /** Call `fn` when the admin's theme toggle or the OS colour scheme changes (to recolour canvases). */
  function onTheme(fn) {
    new MutationObserver(function () {
      fn();
    }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    if (window.matchMedia) window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", fn);
  }

  function number(value) {
    if (value === null || value === undefined || value === "") return "";
    return new Intl.NumberFormat(document.documentElement.lang || undefined).format(value);
  }

  function dateTime(value) {
    if (!value) return "";
    var date = new Date(value);
    if (isNaN(date)) return String(value);
    return date.toLocaleString(document.documentElement.lang || undefined, {
      year: "numeric",
      month: "short",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function relative(value) {
    if (!value) return "";
    var seconds = (new Date(value).getTime() - Date.now()) / 1000;
    var units = [
      ["year", 31536000],
      ["month", 2592000],
      ["day", 86400],
      ["hour", 3600],
      ["minute", 60],
      ["second", 1],
    ];
    var format = new Intl.RelativeTimeFormat(document.documentElement.lang || undefined, { numeric: "auto" });
    for (var i = 0; i < units.length; i++) {
      if (Math.abs(seconds) >= units[i][1] || units[i][0] === "second") {
        return format.format(Math.round(seconds / units[i][1]), units[i][0]);
      }
    }
    return "";
  }

  function escape(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  window.Console = {
    api: api,
    css: css,
    series: series,
    onTheme: onTheme,
    number: number,
    dateTime: dateTime,
    relative: relative,
    escape: escape,
    csrfToken: csrfToken,
  };
})();
