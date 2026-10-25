(function () {
  "use strict";

  var mapEl = document.getElementById("map");
  var statusEl = document.getElementById("map-status");
  var COLOURS = ["#1f6fb4", "#b4471f", "#2e7d32", "#7b1fa2", "#c62828", "#00838f"];

  var map = L.map(mapEl);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
  }).addTo(map);
  map.setView([0, 0], 2);

  function popupFor(device, point, formatter) {
    var box = document.createElement("div");
    var lines = [
      device,
      formatter.format(new Date(point.t * 1000)),
      point.lat.toFixed(6) + ", " + point.lon.toFixed(6),
      "Source " + point.source
    ];
    if (point.speed !== null) lines.push("Speed " + point.speed.toFixed(1) + " kn");
    if (point.course !== null) lines.push("Course " + point.course.toFixed(1) + "\u00b0");
    lines.forEach(function (text, i) {
      var p = document.createElement(i === 0 ? "strong" : "div");
      p.textContent = text;
      box.appendChild(p);
    });
    return box;
  }

  function draw(data) {
    var byDevice = {};
    data.points.forEach(function (p) {
      (byDevice[p.device] = byDevice[p.device] || []).push(p);
    });
    var names = Object.keys(byDevice).sort();
    if (names.length === 0) {
      statusEl.textContent = "No positions in this range. Check the device is connected and has a valid fix.";
      map.setView([0, 0], 2);
      return;
    }

    var formatter = new Intl.DateTimeFormat(undefined, {
      timeZone: data.tz, dateStyle: "short", timeStyle: "medium"
    });
    var bounds = [];
    var total = 0;
    names.forEach(function (name, index) {
      var points = byDevice[name];
      var colour = COLOURS[index % COLOURS.length];
      var latlngs = points.map(function (p) { return [p.lat, p.lon]; });
      total += points.length;
      bounds = bounds.concat(latlngs);
      L.polyline(latlngs, { color: colour, weight: 3, opacity: 0.8 }).addTo(map);
      var last = points[points.length - 1];
      L.circleMarker([last.lat, last.lon], {
        radius: 7, color: colour, fillColor: colour, fillOpacity: 1, weight: 2
      }).bindPopup(popupFor(name, last, formatter)).addTo(map);
    });

    if (bounds.length === 1) {
      map.setView(bounds[0], 15);
    } else {
      map.fitBounds(bounds, { padding: [30, 30], maxZoom: 16 });
    }
    var first = data.points[0];
    var last = data.points[data.points.length - 1];
    statusEl.textContent = total + " positions from " + names.length + " device(s), " +
      formatter.format(new Date(first.t * 1000)) + " to " + formatter.format(new Date(last.t * 1000)) +
      ". The last position of each device is marked.";
  }

  fetch("api/positions" + window.location.search, { credentials: "same-origin" })
    .then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    })
    .then(draw)
    .catch(function (error) {
      statusEl.textContent = "Could not load positions: " + error.message;
    });
})();
