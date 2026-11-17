/*
 * IP-035: the vector explorer (Alpine component + ECharts).
 * Server: apps/console/views/vectors.py — PCA happens there; this file only draws and asks.
 */
(function () {
  "use strict";

  window.vectorsPage = function (config) {
    var chart = null;

    return {
      source: "entry",
      colour: "catalog",
      map: null,
      loadingMap: false,
      mapError: "",
      coverage: [],
      service: {},
      query: "",
      results: [],
      neighbourData: null,
      neighbourError: "",
      passageQuery: "",
      passages: null,
      passageError: "",
      graphUrl: config.graphUrl,

      init: function () {
        var self = this;
        chart = echarts.init(this.$refs.chart, null, { renderer: "canvas" });
        chart.on("click", function (params) {
          if (params.data && params.data.entry) self.pick(params.data.entry);
        });
        window.addEventListener("resize", function () {
          chart.resize();
        });
        Console.onTheme(function () {
          self.draw();
        });
        Console.api(config.coverageUrl)
          .then(function (data) {
            self.coverage = data.items;
            self.service = data.embedding_service || {};
          })
          .catch(function () {});
        this.loadMap();
        if (config.entry) this.pick(config.entry);
      },

      loadMap: function () {
        var self = this;
        this.loadingMap = true;
        this.mapError = "";
        Console.api(config.mapUrl + "?source=" + this.source + "&colour=" + this.colour)
          .then(function (data) {
            self.map = data;
            self.draw();
          })
          .catch(function (error) {
            self.mapError = error.message;
          })
          .finally(function () {
            self.loadingMap = false;
          });
      },

      mapSummary: function () {
        if (!this.map) return "";
        var text = Console.number(this.map.shown) + " / " + Console.number(this.map.total);
        if (this.map.sampled) text += " (sample)";
        var variance = this.map.explained_variance || [0, 0];
        text += " · PC1 " + Math.round(variance[0] * 100) + " %, PC2 " + Math.round(variance[1] * 100) + " %";
        if (this.map.excluded_other_models) text += " · " + this.map.excluded_other_models + " of other models left out";
        return text;
      },

      draw: function () {
        if (!chart || !this.map) return;
        var palette = Console.series();
        var ink = Console.css("--e-ink");
        var muted = Console.css("--e-muted");
        var rule = Console.css("--e-rule");
        var paper = Console.css("--e-paper");
        var accent = Console.css("--e-accent");
        var byGroup = {};
        this.map.points.forEach(function (point) {
          (byGroup[point.group] = byGroup[point.group] || []).push({
            value: [point.x, point.y],
            name: point.title,
            entry: point.entry_id,
          });
        });
        var series = this.map.groups.map(function (group, index) {
          return {
            name: group.label + " (" + group.count + ")",
            type: "scatter",
            symbolSize: 8,
            data: byGroup[group.key] || [],
            itemStyle: { color: palette[index % palette.length], opacity: 0.85 },
            emphasis: { focus: "series", itemStyle: { borderColor: ink, borderWidth: 1 } },
          };
        });
        var highlighted = this.highlightData();
        series.push({
          name: "neighbours",
          type: "scatter",
          symbolSize: 16,
          data: highlighted,
          itemStyle: { color: "transparent", borderColor: accent, borderWidth: 2 },
          z: 10,
          tooltip: { show: true },
        });
        chart.setOption(
          {
            backgroundColor: paper,
            textStyle: { fontFamily: Console.css("--e-font-text"), color: ink },
            animation: false,
            grid: { left: 40, right: 20, top: 20, bottom: 80 },
            tooltip: {
              trigger: "item",
              backgroundColor: paper,
              borderColor: rule,
              textStyle: { color: ink },
              formatter: function (params) {
                return Console.escape(params.name || "") + "<br><span style='color:" + muted + "'>" + Console.escape(params.seriesName) + "</span>";
              },
            },
            legend: {
              type: "scroll",
              bottom: 0,
              textStyle: { color: ink },
              pageTextStyle: { color: muted },
              data: series
                .filter(function (item) {
                  return item.name !== "neighbours";
                })
                .map(function (item) {
                  return item.name;
                }),
            },
            dataZoom: [
              { type: "inside", xAxisIndex: 0, filterMode: "empty" },
              { type: "inside", yAxisIndex: 0, filterMode: "empty" },
            ],
            xAxis: { type: "value", scale: true, axisLabel: { color: muted }, splitLine: { lineStyle: { color: rule } }, axisLine: { lineStyle: { color: rule } } },
            yAxis: { type: "value", scale: true, axisLabel: { color: muted }, splitLine: { lineStyle: { color: rule } }, axisLine: { lineStyle: { color: rule } } },
            series: series,
          },
          true
        );
      },

      highlightData: function () {
        if (!this.map || !this.neighbourData) return [];
        var wanted = {};
        wanted[this.neighbourData.entry.id] = true;
        this.neighbourData.items.forEach(function (item) {
          wanted[item.id] = true;
        });
        return this.map.points
          .filter(function (point) {
            return wanted[point.entry_id];
          })
          .map(function (point) {
            return { value: [point.x, point.y], name: point.title, entry: point.entry_id };
          });
      },

      searchEntries: function () {
        var self = this;
        if (this.query.trim().length < 2) {
          this.results = [];
          return;
        }
        Console.api(config.entriesUrl + "?q=" + encodeURIComponent(this.query.trim()))
          .then(function (data) {
            self.results = data.results;
          })
          .catch(function () {});
      },

      pick: function (entryId) {
        var self = this;
        this.results = [];
        this.neighbourError = "";
        Console.api(config.neighboursUrl + "?source=" + this.source + "&entry=" + encodeURIComponent(entryId) + "&k=12")
          .then(function (data) {
            self.neighbourData = data;
            self.draw();
          })
          .catch(function (error) {
            self.neighbourError = error.message;
          });
      },

      searchPassages: function () {
        var self = this;
        var query = this.passageQuery.trim();
        if (query.length < 3) return;
        this.passageError = "";
        Console.api(config.passagesUrl + "?q=" + encodeURIComponent(query))
          .then(function (data) {
            self.passages = data;
          })
          .catch(function (error) {
            self.passageError = error.message;
          });
      },
    };
  };
})();
