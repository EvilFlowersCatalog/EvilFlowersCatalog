/*
 * IP-035: the live jobs dashboard (Alpine component `jobsPage`, ECharts charts).
 * Polls the console API every POLL_MS while the tab is visible and "Live" is on.
 */
(function () {
  "use strict";

  var POLL_MS = 5000;
  // Chart instances by ref name; deliberately not in the Alpine component (see `chart()`).
  var CHARTS = {};
  var JOB_STATES = ["pending", "blocked", "queued", "running", "cancelling", "succeeded", "failed", "cancelled"];
  var RUN_STATUSES = ["queued", "received", "started", "retrying", "succeeded", "failed", "revoked", "rejected", "lost"];

  // Colour per status family, as token names (resolved per theme).
  var STATUS_TOKEN = {
    succeeded: "--e-ok",
    failed: "--e-accent",
    rejected: "--e-accent",
    lost: "--e-series-7",
    revoked: "--e-series-8",
    cancelled: "--e-series-8",
    cancelling: "--e-series-8",
    running: "--e-info",
    started: "--e-info",
    received: "--e-series-6",
    queued: "--e-series-6",
    retrying: "--e-warn",
    pending: "--e-warn",
    blocked: "--e-series-5",
  };

  function statusColor(status) {
    return Console.css(STATUS_TOKEN[status] || "--e-muted");
  }

  function baseTextStyle() {
    return { fontFamily: Console.css("--e-font-text") || "sans-serif", color: Console.css("--e-ink") };
  }

  function axisStyle() {
    var muted = Console.css("--e-muted");
    var rule = Console.css("--e-rule");
    return {
      axisLine: { lineStyle: { color: rule } },
      axisTick: { lineStyle: { color: rule } },
      axisLabel: { color: muted, fontSize: 11 },
      splitLine: { lineStyle: { color: rule } },
    };
  }

  function tooltip(extra) {
    return Object.assign(
      {
        backgroundColor: Console.css("--e-paper"),
        borderColor: Console.css("--e-rule-strong"),
        textStyle: { color: Console.css("--e-ink"), fontSize: 12 },
      },
      extra || {}
    );
  }

  window.jobsPage = function () {
    return {
      urls: {},
      readOnly: false,
      live: true,
      paused: document.hidden,
      timer: null,
      hours: 24,
      jobStates: JOB_STATES,
      runStatuses: RUN_STATUSES,
      summary: {},
      series: null,
      queues: null,
      list: { rows: [], page: 1, has_next: false },
      filters: { view: "jobs", kind: "", state: "", q: "" },
      loading: { list: false },
      error: "",
      updatedAt: null,
      drawer: null,
      acting: false,
      actionResult: "",
      actionError: "",
      charts: {},
      inflight: null,

      init: function () {
        var el = this.$el;
        this.urls = {
          summary: el.dataset.summary,
          timeseries: el.dataset.timeseries,
          list: el.dataset.list,
          queues: el.dataset.queues,
          base: el.dataset.summary.replace(/summary\/$/, ""),
        };
        this.readOnly = el.dataset.readOnly === "1";
        var self = this;
        document.addEventListener("visibilitychange", function () {
          self.paused = document.hidden;
          if (!self.paused && self.live) self.refresh(false);
          self.schedule();
        });
        Console.onTheme(function () {
          self.renderCharts();
        });
        window.addEventListener("resize", function () {
          Object.values(self.charts).forEach(function (chart) {
            chart.resize();
          });
        });
        this.$nextTick(function () {
          self.refresh(true);
          self.schedule();
        });
      },

      schedule: function () {
        var self = this;
        if (this.timer) clearInterval(this.timer);
        this.timer = null;
        if (this.live && !this.paused) {
          this.timer = setInterval(function () {
            self.refresh(false);
          }, POLL_MS);
        }
      },

      refresh: function (all) {
        var self = this;
        var tasks = [this.loadSummary(), this.loadList(this.list.page, true), this.loadQueues()];
        // Charts aggregate a whole day: every third tick is plenty, or on demand.
        this.tick = (this.tick || 0) + 1;
        if (all || this.tick % 3 === 1) tasks.push(this.loadCharts());
        if (this.drawer) tasks.push(this.reloadDrawer());
        return Promise.all(tasks).then(function () {
          self.updatedAt = new Date();
        });
      },

      handle: function (promise) {
        var self = this;
        return promise
          .then(function (data) {
            self.error = "";
            return data;
          })
          .catch(function (error) {
            self.error = error.message;
            return null;
          });
      },

      loadSummary: function () {
        var self = this;
        return this.handle(Console.api(this.urls.summary)).then(function (data) {
          if (data) self.summary = data;
        });
      },

      loadQueues: function () {
        var self = this;
        return Console.api(this.urls.queues)
          .then(function (data) {
            self.queues = data;
          })
          .catch(function () {
            self.queues = { reachable: false, queues: [] };
          });
      },

      loadCharts: function () {
        var self = this;
        return this.handle(Console.api(this.urls.timeseries + "?hours=" + this.hours)).then(function (data) {
          if (!data) return;
          self.series = data;
          self.renderCharts();
        });
      },

      loadList: function (page, quiet) {
        var self = this;
        var params = new URLSearchParams({ view: this.filters.view, page: page || 1 });
        if (this.filters.kind) params.set("kind", this.filters.kind);
        if (this.filters.state) params.set("state", this.filters.state);
        if (this.filters.q) params.set("q", this.filters.q);
        if (!quiet) this.loading.list = true;
        return this.handle(Console.api(this.urls.list + "?" + params.toString())).then(function (data) {
          self.loading.list = false;
          if (data) self.list = data;
        });
      },

      setView: function (view) {
        if (this.filters.view === view) return;
        this.filters.view = view;
        this.filters.state = "";
        this.loadList(1);
      },

      pickState: function (state) {
        this.filters.view = "jobs";
        this.filters.state = this.filters.state === state ? "" : state;
        this.loadList(1);
      },

      // --- drawer ------------------------------------------------------------------------------------

      open: function (row) {
        if (this.filters.view === "jobs") this.openJob(row.id);
        else this.openRun(row.id);
      },

      openJob: function (id) {
        var self = this;
        this.actionResult = "";
        this.actionError = "";
        return Console.api(this.urls.base + id + "/")
          .then(function (data) {
            data.mode = "job";
            self.drawer = data;
          })
          .catch(function (error) {
            self.error = error.message;
          });
      },

      openRun: function (id) {
        var self = this;
        this.actionResult = "";
        this.actionError = "";
        return Console.api(this.urls.base + "runs/" + id + "/")
          .then(function (data) {
            data.mode = "run";
            self.drawer = data;
          })
          .catch(function (error) {
            self.error = error.message;
          });
      },

      reloadDrawer: function () {
        if (!this.drawer) return Promise.resolve();
        var id = this.drawer.id;
        var mode = this.drawer.mode;
        var self = this;
        var url = mode === "job" ? this.urls.base + id + "/" : this.urls.base + "runs/" + id + "/";
        return Console.api(url)
          .then(function (data) {
            if (self.drawer && self.drawer.id === id) {
              data.mode = mode;
              self.drawer = data;
            }
          })
          .catch(function () {});
      },

      close: function () {
        this.drawer = null;
      },

      actionLabel: function (action) {
        return { retry: "Retry", cancel: "Cancel", reconcile: "Reconcile now" }[action] || action;
      },

      act: function (action) {
        var messages = {
          retry: "Apply this job again (a new generation if it has settled)?",
          cancel: "Cancel this job? A queued run is revoked, a running one stopped where the kind allows it.",
          reconcile: "Let the reconciler take the next transition now?",
        };
        if (!window.confirm(messages[action] || action + "?")) return;
        var self = this;
        this.acting = true;
        this.actionResult = "";
        this.actionError = "";
        Console.api(this.urls.base + this.drawer.id + "/action/", { method: "POST", body: { action: action } })
          .then(function (data) {
            self.actionResult = action + " → " + data.state + " (desired " + data.desired_state + ", generation " + data.generation + ")";
            return Promise.all([self.reloadDrawer(), self.loadSummary(), self.loadList(self.list.page, true)]);
          })
          .catch(function (error) {
            self.actionError = error.message;
          })
          .finally(function () {
            self.acting = false;
          });
      },

      traceback: function () {
        if (!this.drawer) return "";
        if (this.drawer.traceback) return this.drawer.traceback;
        if (this.drawer.current_run && this.drawer.current_run.traceback) return this.drawer.current_run.traceback;
        return "";
      },

      facts: function () {
        var d = this.drawer;
        if (!d) return [];
        var esc = Console.escape;
        var rows = [];
        var link = function (url, text) {
          return url ? '<a href="' + esc(url) + '">' + esc(text) + "</a>" : esc(text);
        };
        if (d.mode === "job") {
          if (d.entry) rows.push(["Entry", link(d.entry_url, d.entry)]);
          if (d.catalog) rows.push(["Catalog", esc(d.catalog)]);
          if (d.subject) rows.push(["Subject", '<span class="console-mono">' + esc(d.subject) + " " + esc(d.subject_id || "") + "</span>"]);
          rows.push(["Generation", esc(d.observed_generation + " / " + d.generation)]);
          rows.push(["Attempts", esc(d.attempts)]);
          if (d.requested_by) rows.push(["Requested by", esc(d.requested_by)]);
          rows.push(["Last change", esc(Console.dateTime(d.last_transition_at))]);
          var run = d.current_run;
          if (run) {
            rows.push(["Current run", '<span class="console-mono">' + esc(run.worker || run.queue || "—") + "</span> · " + esc(run.status)]);
            if (run.trace_id) rows.push(["Trace", link(run.trace_url, run.trace_id)]);
          }
        } else {
          rows.push(["Task", '<span class="console-mono">' + esc(d.name) + "</span>"]);
          rows.push(["Queue", esc(d.queue || "—")]);
          rows.push(["Worker", '<span class="console-mono">' + esc(d.worker || "—") + "</span>"]);
          rows.push(["Attempts", esc(d.attempts)]);
          rows.push(["Runtime", esc(this.seconds(d.runtime))]);
          ["sent_at", "received_at", "started_at", "finished_at"].forEach(function (key) {
            if (d[key]) rows.push([key.replace("_at", ""), esc(Console.dateTime(d[key]))]);
          });
          if (d.trace_id) rows.push(["Trace", link(d.trace_url, d.trace_id)]);
        }
        return rows;
      },

      eventClass: function (type) {
        var t = String(type || "");
        if (/fail|reject|lost|error/.test(t)) return "bad";
        if (/succeed|done|success/.test(t)) return "ok";
        if (/revoke|cancel/.test(t)) return "muted";
        return "";
      },

      // --- formatting --------------------------------------------------------------------------------

      badge: function (state) {
        if (["succeeded"].indexOf(state) >= 0) return "ok";
        if (["failed", "rejected", "lost"].indexOf(state) >= 0) return "bad";
        if (["running", "started", "received", "queued"].indexOf(state) >= 0) return "info";
        if (["pending", "blocked", "retrying", "cancelling"].indexOf(state) >= 0) return "warn";
        return "muted";
      },

      percent: function (value) {
        if (value === null || value === undefined) return "—";
        return (value * 100).toFixed(value < 0.1 ? 1 : 0) + " %";
      },

      seconds: function (value) {
        if (value === null || value === undefined) return "—";
        if (value < 1) return Math.round(value * 1000) + " ms";
        if (value < 120) return value.toFixed(1) + " s";
        if (value < 7200) return (value / 60).toFixed(1) + " min";
        return (value / 3600).toFixed(1) + " h";
      },

      pretty: function (value) {
        try {
          return JSON.stringify(value, null, 2);
        } catch (error) {
          return String(value);
        }
      },

      updatedLabel: function () {
        if (!this.updatedAt) return "";
        return "updated " + this.updatedAt.toLocaleTimeString();
      },

      // --- charts ------------------------------------------------------------------------------------

      chart: function (name) {
        var el = this.$refs[name];
        if (!el || !window.echarts) return null;
        // ECharts instances live outside Alpine's reactive state: a proxied instance breaks ECharts'
        // internals on the next setOption ("Cannot read properties of undefined (reading 'type')").
        if (!CHARTS[name]) CHARTS[name] = echarts.init(el, null, { renderer: "canvas" });
        return CHARTS[name];
      },

      renderCharts: function () {
        if (!this.series) return;
        this.renderTimeline();
        this.renderRuntimes();
        this.renderMatrix();
      },

      renderTimeline: function () {
        var chart = this.chart("timeline");
        if (!chart) return;
        var data = this.series;
        var labels = data.buckets.map(function (iso) {
          var d = new Date(iso);
          return data.hours > 24
            ? d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " + d.toLocaleTimeString(undefined, { hour: "2-digit" })
            : d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
        });
        var order = RUN_STATUSES.filter(function (status) {
          return data.series[status];
        });
        Object.keys(data.series).forEach(function (status) {
          if (order.indexOf(status) < 0) order.push(status);
        });
        var axis = axisStyle();
        chart.setOption(
          {
            textStyle: baseTextStyle(),
            animation: false,
            grid: { left: 40, right: 12, top: 30, bottom: 28 },
            legend: { top: 0, textStyle: { color: Console.css("--e-muted"), fontSize: 11 }, itemWidth: 10, itemHeight: 10 },
            tooltip: tooltip({ trigger: "axis", axisPointer: { type: "shadow" } }),
            xAxis: Object.assign({ type: "category", data: labels }, axis, { splitLine: { show: false } }),
            yAxis: Object.assign({ type: "value", minInterval: 1 }, axis),
            series: order.map(function (status) {
              return {
                name: status,
                type: "bar",
                stack: "runs",
                barCategoryGap: "15%",
                itemStyle: { color: statusColor(status) },
                emphasis: { focus: "series" },
                data: data.series[status],
              };
            }),
          },
          true
        );
      },

      renderRuntimes: function () {
        var chart = this.chart("runtimes");
        if (!chart) return;
        var rows = (this.series.runtimes || []).slice(0, 12).reverse();
        var axis = axisStyle();
        chart.setOption(
          {
            textStyle: baseTextStyle(),
            animation: false,
            grid: { left: 8, right: 16, top: 24, bottom: 20, containLabel: true },
            legend: { top: 0, textStyle: { color: Console.css("--e-muted"), fontSize: 11 }, itemWidth: 10, itemHeight: 10 },
            tooltip: tooltip({
              trigger: "axis",
              axisPointer: { type: "shadow" },
              formatter: function (items) {
                var row = rows[items[0].dataIndex];
                return (
                  "<b>" + Console.escape(row.kind) + "</b><br>" + row.runs + " runs<br>p50 " + (row.p50 || 0).toFixed(2) +
                  " s · p95 " + (row.p95 || 0).toFixed(2) + " s · max " + (row.max || 0).toFixed(2) + " s"
                );
              },
            }),
            xAxis: Object.assign({ type: "log", logBase: 10, name: "s", nameTextStyle: { color: Console.css("--e-muted") } }, axis),
            yAxis: Object.assign(
              {
                type: "category",
                data: rows.map(function (row) {
                  return row.kind;
                }),
              },
              axis,
              { axisLabel: { color: Console.css("--e-muted"), fontSize: 11, width: 150, overflow: "truncate" } }
            ),
            series: [
              {
                name: "p50",
                type: "bar",
                itemStyle: { color: Console.css("--e-series-2") },
                data: rows.map(function (row) {
                  return Math.max(row.p50 || 0, 0.001);
                }),
              },
              {
                name: "p95",
                type: "bar",
                itemStyle: { color: Console.css("--e-accent") },
                data: rows.map(function (row) {
                  return Math.max(row.p95 || 0, 0.001);
                }),
              },
            ],
          },
          true
        );
      },

      renderMatrix: function () {
        var chart = this.chart("matrix");
        if (!chart) return;
        var matrix = this.series.matrix;
        var kinds = matrix.kinds;
        var states = matrix.states;
        var max = 1;
        var cells = matrix.cells.map(function (cell) {
          max = Math.max(max, cell[2]);
          return [states.indexOf(cell[1]), kinds.indexOf(cell[0]), cell[2]];
        });
        var axis = axisStyle();
        var self = this;
        chart.setOption(
          {
            textStyle: baseTextStyle(),
            animation: false,
            grid: { left: 8, right: 12, top: 8, bottom: 52, containLabel: true },
            tooltip: tooltip({
              formatter: function (item) {
                return Console.escape(kinds[item.value[1]]) + " · " + states[item.value[0]] + ": <b>" + item.value[2] + "</b>";
              },
            }),
            xAxis: Object.assign({ type: "category", data: states }, axis, {
              splitArea: { show: false },
              axisLabel: { color: Console.css("--e-muted"), fontSize: 10, rotate: 35 },
            }),
            yAxis: Object.assign({ type: "category", data: kinds }, axis, {
              axisLabel: { color: Console.css("--e-muted"), fontSize: 11, width: 150, overflow: "truncate" },
            }),
            visualMap: {
              show: false,
              min: 0,
              max: max,
              inRange: { color: [Console.css("--e-accent-wash"), Console.css("--e-accent")] },
            },
            series: [
              {
                type: "heatmap",
                data: cells,
                label: { show: true, color: Console.css("--e-ink"), fontSize: 10 },
                itemStyle: { borderColor: Console.css("--e-paper"), borderWidth: 2 },
              },
            ],
          },
          true
        );
        chart.off("click");
        chart.on("click", function (item) {
          self.filters.view = "jobs";
          self.filters.kind = kinds[item.value[1]] === "—" ? "" : kinds[item.value[1]];
          self.filters.state = states[item.value[0]];
          self.loadList(1);
        });
      },
    };
  };
})();
