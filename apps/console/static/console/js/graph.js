/*
 * IP-035: the knowledge-graph explorer (Alpine component + Cytoscape.js).
 * Server: apps/console/views/graph.py. Node ids are `<kind>:<key>`; one expansion = one JSON call.
 */
(function () {
  "use strict";

  var NODE_KINDS = ["entry", "section", "concept", "category", "author"];

  function edgeTypes() {
    var element = document.getElementById("graph-edge-types");
    return element ? JSON.parse(element.textContent) : [];
  }

  window.graphPage = function (config) {
    var types = edgeTypes();
    var enabled = {};
    types.forEach(function (type) {
      // Unrelated pairs are noise by default; everything else starts visible.
      enabled[type.key] = type.key !== "UNRELATED";
    });

    var cy = null;

    return {
      searchUrl: config.searchUrl,
      expandUrl: config.expandUrl,
      overviewUrl: config.overviewUrl,
      vectorsUrl: config.vectorsUrl,
      edgeTypes: types,
      enabled: enabled,
      counts: {},
      query: "",
      results: [],
      layout: "cose",
      minP: 0,
      showHidden: false,
      selected: null,
      overview: null,
      loading: false,
      error: "",
      source: "",
      truncated: false,
      empty: true,
      expanded: {},

      init: function () {
        var self = this;
        cy = cytoscape({
          container: this.$refs.canvas,
          elements: [],
          style: this.style(),
          minZoom: 0.1,
          // A handful of nodes would otherwise be fitted at 4x with poster-size labels.
          maxZoom: 1.6,
        });
        cy.on("tap", "node", function (event) {
          self.select(event.target.data());
        });
        cy.on("tap", "edge", function (event) {
          self.select(event.target.data());
        });
        cy.on("dbltap", "node", function (event) {
          self.expand(event.target.id());
        });
        cy.on("tap", function (event) {
          if (event.target === cy) self.selected = null;
        });
        Console.onTheme(function () {
          cy.style(self.style());
        });
        Console.api(this.overviewUrl)
          .then(function (data) {
            self.overview = data;
          })
          .catch(function () {});
        if (config.seed) this.expand(config.seed);
      },

      nodeColour: function (kind) {
        var series = Console.series();
        var index = NODE_KINDS.indexOf(kind);
        return series[(index < 0 ? 7 : index) % series.length];
      },

      edgeColour: function (type) {
        var series = Console.series();
        var index = 0;
        for (var i = 0; i < this.edgeTypes.length; i++) if (this.edgeTypes[i].key === type) index = i;
        return series[index % series.length];
      },

      style: function () {
        var self = this;
        var ink = Console.css("--e-ink");
        var paper = Console.css("--e-paper");
        var accent = Console.css("--e-accent");
        var styles = [
          {
            selector: "node",
            style: {
              label: "data(label)",
              "font-family": Console.css("--e-font-text") || "sans-serif",
              "font-size": 10,
              color: ink,
              "text-valign": "bottom",
              "text-margin-y": 4,
              "text-wrap": "ellipsis",
              "text-max-width": 140,
              "text-background-color": paper,
              "text-background-opacity": 0.85,
              "text-background-padding": 1,
              width: 14,
              height: 14,
              "border-width": 1,
              "border-color": paper,
            },
          },
          { selector: "node[kind = 'entry']", style: { width: 22, height: 22, shape: "round-rectangle" } },
          { selector: "node[kind = 'category']", style: { shape: "diamond", width: 18, height: 18 } },
          { selector: "node[kind = 'concept']", style: { shape: "ellipse", width: 12, height: 12 } },
          { selector: "node[kind = 'author']", style: { shape: "triangle" } },
          { selector: "node.expanded", style: { "border-width": 3, "border-color": ink } },
          { selector: "node:selected", style: { "border-width": 3, "border-color": accent } },
          {
            selector: "edge",
            style: {
              width: "mapData(weight, 0, 1, 1, 4)",
              "curve-style": "bezier",
              opacity: 0.75,
              "target-arrow-shape": "none",
              "arrow-scale": 0.8,
            },
          },
          {
            selector: "edge[type = 'PREREQUISITE_OF'], edge[type = 'EXAMPLE_OF'], edge[type = 'CITES']",
            style: { "target-arrow-shape": "triangle" },
          },
          { selector: "edge[band = 'hide']", style: { "line-style": "dashed", opacity: 0.45 } },
          { selector: "edge[band = 'review']", style: { "line-style": "dotted" } },
          { selector: "edge:selected", style: { opacity: 1, width: 4 } },
          { selector: ".filtered", style: { display: "none" } },
        ];
        NODE_KINDS.forEach(function (kind) {
          styles.push({ selector: "node[kind = '" + kind + "']", style: { "background-color": self.nodeColour(kind) } });
        });
        this.edgeTypes.forEach(function (type) {
          var colour = self.edgeColour(type.key);
          styles.push({
            selector: "edge[type = '" + type.key + "']",
            style: { "line-color": colour, "target-arrow-color": colour },
          });
        });
        return styles;
      },

      search: function () {
        var self = this;
        if (this.query.trim().length < 2) {
          this.results = [];
          return;
        }
        Console.api(this.searchUrl + "?q=" + encodeURIComponent(this.query.trim()))
          .then(function (data) {
            self.results = data.results;
          })
          .catch(function (error) {
            self.error = error.message;
          });
      },

      seedWith: function (item) {
        this.results = [];
        this.query = "";
        this.expand(item.id);
      },

      expand: function (nodeId) {
        var self = this;
        this.loading = true;
        this.error = "";
        var url =
          this.expandUrl + "?node=" + encodeURIComponent(nodeId) + (this.showHidden ? "&hidden=1" : "");
        return Console.api(url)
          .then(function (data) {
            self.merge(data, nodeId);
            self.expanded[nodeId] = true;
            var node = cy.getElementById(nodeId);
            if (node.nonempty()) {
              node.addClass("expanded");
              self.select(node.data());
            }
          })
          .catch(function (error) {
            self.error = error.message;
          })
          .finally(function () {
            self.loading = false;
          });
      },

      merge: function (data, around) {
        var added = [];
        data.nodes.forEach(function (node) {
          if (cy.getElementById(node.id).empty()) added.push({ group: "nodes", data: node });
        });
        data.edges.forEach(function (edge) {
          if (cy.getElementById(edge.id).empty()) {
            added.push({
              group: "edges",
              data: Object.assign({}, edge, { weight: edge.p === null || edge.p === undefined ? 0.4 : edge.p }),
            });
          }
        });
        var anchor = cy.getElementById(around);
        var position = anchor.nonempty() ? anchor.position() : { x: 0, y: 0 };
        var elements = cy.add(
          added.map(function (item) {
            if (item.group === "nodes") {
              item.position = { x: position.x + (Math.random() - 0.5) * 120, y: position.y + (Math.random() - 0.5) * 120 };
            }
            return item;
          })
        );
        this.source = data.source + (data.age_available ? " · AGE on" : "");
        this.truncated = data.truncated;
        this.empty = cy.elements().empty();
        this.recount();
        this.applyFilters();
        if (elements.nonempty()) this.runLayout();
      },

      runLayout: function () {
        if (!cy || cy.elements().empty()) return;
        var options = { name: this.layout, animate: true, animationDuration: 400, fit: true, padding: 30 };
        if (this.layout === "cose") {
          Object.assign(options, { randomize: false, nodeRepulsion: 8000, idealEdgeLength: 80, numIter: 800 });
        }
        if (this.layout === "concentric") {
          options.concentric = function (node) {
            return node.degree();
          };
          options.levelWidth = function () {
            return 2;
          };
        }
        cy.elements(":visible").layout(options).run();
      },

      applyFilters: function () {
        var self = this;
        if (!cy) return;
        cy.edges().forEach(function (edge) {
          var type = edge.data("type");
          var p = edge.data("p");
          var hide = self.enabled[type] === false || (self.minP > 0 && p !== null && p !== undefined && p < self.minP);
          edge.toggleClass("filtered", hide);
        });
        // A node whose every edge is filtered stays visible only if it was expanded or is isolated by design.
        cy.nodes().forEach(function (node) {
          var visibleEdges = node.connectedEdges().filter(function (edge) {
            return !edge.hasClass("filtered");
          });
          node.toggleClass("filtered", node.degree() > 0 && visibleEdges.empty() && !self.expanded[node.id()]);
        });
      },

      recount: function () {
        var counts = {};
        cy.edges().forEach(function (edge) {
          counts[edge.data("type")] = (counts[edge.data("type")] || 0) + 1;
        });
        this.counts = counts;
      },

      reloadExpanded: function () {
        var self = this;
        var ids = Object.keys(this.expanded);
        cy.elements().remove();
        this.expanded = {};
        ids.reduce(function (chain, id) {
          return chain.then(function () {
            return self.expand(id);
          });
        }, Promise.resolve());
      },

      select: function (data) {
        this.selected = Object.assign({}, data);
      },

      selectedRows: function () {
        if (!this.selected) return [];
        var skip = { label: 1, kind: 1, url: 1, weight: 1 };
        var rows = [];
        Object.keys(this.selected).forEach(function (key) {
          if (skip[key]) return;
          var value = this.selected[key];
          if (value === null || value === undefined || value === "") return;
          rows.push([key, String(value)]);
        }, this);
        if (this.selected.kind && cy) {
          var node = cy.getElementById(this.selected.id);
          if (node.nonempty()) rows.push(["degree", String(node.degree())]);
        }
        return rows;
      },

      remove: function (nodeId) {
        cy.getElementById(nodeId).remove();
        delete this.expanded[nodeId];
        this.selected = null;
        this.empty = cy.elements().empty();
        this.recount();
      },

      fit: function () {
        if (cy) cy.fit(undefined, 30);
      },

      clear: function () {
        cy.elements().remove();
        this.expanded = {};
        this.selected = null;
        this.empty = true;
        this.truncated = false;
        this.counts = {};
      },

      stats: function () {
        // Reading the reactive `counts` and `empty` makes Alpine re-evaluate this after every merge or removal.
        var touched = this.counts && this.empty;
        if (!cy || touched === undefined) return "";
        return cy.nodes().length + " nodes · " + cy.edges().length + " edges";
      },
    };
  };
})();
