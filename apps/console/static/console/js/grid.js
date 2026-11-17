/*
 * IP-035: the console data grid. An Alpine.js component around Tabulator in remote mode: every page,
 * sort and header filter goes to `console/api/grid/<app>/<model>/` (apps/console/views/grid.py).
 * Column definitions come from the page (`#grid-columns`); visibility and density persist per model
 * in localStorage.
 */
(function () {
  "use strict";

  // The Tabulator instance stays outside Alpine's reactive state: a proxied instance makes every internal
  // write go through Alpine's reactivity (slow, and it breaks libraries that compare identities).
  var TABLE = { instance: null };

  function readStore(key) {
    try {
      return JSON.parse(window.localStorage.getItem(key) || "{}") || {};
    } catch (error) {
      return {};
    }
  }

  function writeStore(key, value) {
    try {
      window.localStorage.setItem(key, JSON.stringify(value));
    } catch (error) {
      /* storage may be blocked; the grid works without it */
    }
  }

  /** Tabulator's nested params ({sort: [{field, dir}]}) → `sort[0][field]=…&sort[0][dir]=…`. */
  function serialise(params, prefix, out) {
    out = out || [];
    Object.keys(params).forEach(function (key) {
      var value = params[key];
      var name = prefix ? prefix + "[" + key + "]" : key;
      if (value === undefined || value === null || value === "") return;
      if (typeof value === "object") {
        serialise(value, name, out);
      } else {
        out.push(encodeURIComponent(name) + "=" + encodeURIComponent(value));
      }
    });
    return out;
  }

  function query(params) {
    return serialise(params).join("&");
  }

  function formatter(column) {
    var esc = Console.escape;
    switch (column.type) {
      case "fk":
        return function (cell) {
          var value = cell.getValue();
          if (!value) return '<span class="console-muted">—</span>';
          var label = esc(value.label || value.id);
          return value.url ? '<a href="' + esc(value.url) + '">' + label + "</a>" : label;
        };
      case "bool":
        return function (cell) {
          var value = cell.getValue();
          if (value === null || value === undefined) return '<span class="console-muted">—</span>';
          return value ? '<span class="console-badge ok">yes</span>' : '<span class="console-badge muted">no</span>';
        };
      case "choice":
        var labels = {};
        (column.choices || []).forEach(function (pair) {
          labels[pair[0]] = pair[1];
        });
        return function (cell) {
          var value = cell.getValue();
          if (value === null || value === undefined || value === "") return '<span class="console-muted">—</span>';
          return '<span class="console-chip">' + esc(labels[value] || value) + "</span>";
        };
      case "datetime":
        return function (cell) {
          var value = cell.getValue();
          return value ? '<span title="' + esc(value) + '">' + esc(Console.dateTime(value)) + "</span>" : "";
        };
      case "number":
        return function (cell) {
          var value = cell.getValue();
          return value === null || value === undefined ? "" : esc(Console.number(value));
        };
      case "uuid":
        return function (cell) {
          var value = cell.getValue();
          return value ? '<span class="console-mono" title="' + esc(value) + '">' + esc(String(value).slice(0, 8)) + "…</span>" : "";
        };
      default:
        return function (cell) {
          var value = cell.getValue();
          if (value === null || value === undefined) return "";
          var text = esc(value);
          return column.type === "json" || column.type === "text"
            ? '<span class="console-grid-long" title="' + text + '">' + text + "</span>"
            : text;
        };
    }
  }

  function headerFilter(column, big) {
    if (!column.filterable) return {};
    if (column.type === "choice") {
      var values = { "": "—" };
      (column.choices || []).forEach(function (pair) {
        values[pair[0]] = pair[1];
      });
      return { headerFilter: "list", headerFilterParams: { values: values, clearable: true } };
    }
    if (column.type === "bool") {
      return { headerFilter: "list", headerFilterParams: { values: { "": "—", true: "yes", false: "no" }, clearable: true } };
    }
    var placeholder = "";
    if (column.type === "number") placeholder = "=, >=, <…";
    else if (column.type === "date" || column.type === "datetime") placeholder = "YYYY-MM-DD..";
    else if (column.type === "fk" || column.type === "uuid") placeholder = "id";
    else if (big) placeholder = "exact";
    return { headerFilter: "input", headerFilterPlaceholder: placeholder, headerFilterLiveFilter: false };
  }

  window.consoleGrid = function (config) {
    return {
      q: "",
      total: null,
      estimated: false,
      filters: 0,
      error: "",
      compact: false,
      columnsOpen: false,
      columns: [],
      lastParams: {},

      init: function () {
        var self = this;
        var stored = readStore(config.storageKey);
        var visibility = stored.columns || {};
        this.compact = !!stored.compact;
        this.columns = JSON.parse(document.getElementById("grid-columns").textContent).map(function (column) {
          if (Object.prototype.hasOwnProperty.call(visibility, column.field)) column.visible = visibility[column.field];
          column.defaultVisible = column.visible;
          return column;
        });

        var definitions = this.columns.map(function (column) {
          return Object.assign(
            {
              title: column.title,
              field: column.field,
              visible: column.visible,
              headerSort: column.sortable,
              formatter: formatter(column),
              minWidth: 70,
              maxWidth: column.type === "json" || column.type === "text" ? 420 : 320,
              tooltip: false,
            },
            headerFilter(column, config.big)
          );
        });

        TABLE.instance = new Tabulator(this.$refs.table, {
          columns: definitions,
          layout: "fitDataFill",
          placeholder: "No rows match.",
          pagination: true,
          paginationMode: "remote",
          paginationSize: stored.size || 50,
          paginationSizeSelector: [25, 50, 100, 250, 500],
          paginationButtonCount: 5,
          sortMode: "remote",
          filterMode: "remote",
          headerFilterLiveFilterDelay: 500,
          ajaxURL: config.api,
          ajaxRequestFunc: function (url, ajaxConfig, params) {
            params = Object.assign({}, params);
            if (self.q) params.q = self.q;
            self.lastParams = params;
            self.filters = (params.filter || []).length;
            self.error = "";
            return Console.api(url + "?" + query(params)).then(
              function (response) {
                self.total = response.total;
                self.estimated = !!response.estimated;
                return response;
              },
              function (error) {
                self.error = error.message;
                return { last_page: 1, data: [] };
              }
            );
          },
        });

        TABLE.instance.on("rowClick", function (event, row) {
          if (event.target.closest("a, button, input, select")) return;
          var url = row.getData()._url;
          if (url) window.location.href = url;
        });
        TABLE.instance.on("pageSizeChanged", function (size) {
          self.persist({ size: size });
        });
      },

      persist: function (patch) {
        var stored = readStore(config.storageKey);
        writeStore(config.storageKey, Object.assign(stored, patch));
      },

      reload: function () {
        if (TABLE.instance) TABLE.instance.setData();
      },

      clearFilters: function () {
        this.q = "";
        TABLE.instance.clearHeaderFilter();
        this.reload();
      },

      toggleColumn: function (column, visible) {
        column.visible = visible;
        var target = TABLE.instance.getColumn(column.field);
        if (target) visible ? target.show() : target.hide();
        var map = {};
        this.columns.forEach(function (item) {
          if (item.visible !== item.defaultVisible) map[item.field] = item.visible;
        });
        this.persist({ columns: map });
      },

      resetColumns: function () {
        var self = this;
        this.columns.forEach(function (column) {
          if (column.visible !== column.defaultVisible) self.toggleColumn(column, column.defaultVisible);
        });
        this.persist({ columns: {} });
      },

      toggleDensity: function () {
        this.compact = !this.compact;
        this.persist({ compact: this.compact });
        var table = TABLE.instance;
        this.$nextTick(function () {
          table.redraw(true);
        });
      },

      csvUrl: function () {
        var params = Object.assign({}, this.lastParams, { format: "csv" });
        delete params.page;
        delete params.size;
        return config.api + "?" + query(params);
      },
    };
  };
})();
