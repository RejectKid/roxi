const map = document.querySelector("[data-history-map]");
const canvas = map.querySelector("canvas");
const markers = map.querySelector("[data-map-markers]");
const data = JSON.parse(document.querySelector("#history-map-data").textContent);
const colors = {site: "#155e4b", vehicle_observation: "#1d4ed8", weather_observation: "#704baa"};

function coordinates(geometry) {
  if (geometry.type === "Point") return [geometry.coordinates];
  if (geometry.type === "LineString") return geometry.coordinates;
  return geometry.coordinates.flat();
}

const positions = [...data.shapes.flatMap(shape => coordinates(shape.geometry)), ...data.points.map(point => point.position)];

function projector(width, height) {
  const bounds = positions.reduce((box, [x, y]) =>
    [Math.min(box[0], x), Math.min(box[1], y), Math.max(box[2], x), Math.max(box[3], y)],
  [Infinity, Infinity, -Infinity, -Infinity]);
  const [west, south, east, north] = bounds;
  const longitudeScale = Math.cos((south + north) * Math.PI / 360);
  const spanX = Math.max((east - west) * longitudeScale, 0.0001);
  const spanY = Math.max(north - south, 0.0001);
  const scale = Math.max(0, Math.min((width - 60) / spanX, (height - 60) / spanY));
  return ([x, y]) => [
    width / 2 + (x - (east + west) / 2) * longitudeScale * scale,
    height / 2 - (y - (north + south) / 2) * scale,
  ];
}

function trace(context, sequence, project) {
  sequence.forEach((position, index) => {
    const [x, y] = project(position);
    if (index === 0) context.moveTo(x, y);
    else context.lineTo(x, y);
  });
}

function drawFeatures(context, project) {
  for (const feature of data.shapes) {
    const {geometry} = feature;
    context.beginPath();
    if (geometry.type === "Polygon") {
      for (const ring of geometry.coordinates) {
        trace(context, ring, project);
        context.closePath();
      }
      context.fillStyle = "#cddfd3";
      context.fill("evenodd");
    } else if (geometry.type === "LineString") {
      trace(context, geometry.coordinates, project);
    } else {
      const [x, y] = project(geometry.coordinates);
      context.arc(x, y, 4, 0, 2 * Math.PI);
    }
    context.strokeStyle = geometry.type === "LineString" ? "#758b84" : "#5a7b6b";
    context.lineWidth = geometry.type === "LineString" ? 2.5 : 1.3;
    context.stroke();
  }
}

function makeMarker(point) {
  const marker = document.createElement("button");
  marker.className = "history-marker";
  marker.type = "button";
  marker.dataset.observation = point.key;
  marker.style.setProperty("--marker-color", colors[point.kind] || "#26434c");
  marker.title = point.label;
  marker.setAttribute("aria-label", "Locate " + point.label + " in the timeline");
  marker.addEventListener("click", () => {
    const row = document.getElementById(point.key);
    if (!row) return;
    for (const selected of document.querySelectorAll(".history-selected")) selected.classList.remove("history-selected");
    row.classList.add("history-selected");
    row.focus({preventScroll: true});
    row.scrollIntoView({block: "nearest"});
  });
  return marker;
}

const buttons = data.points.map(makeMarker);
markers.append(...buttons);

function render() {
  if (!positions.length) return;
  const {width, height} = map.getBoundingClientRect();
  if (!width || !height) return;
  const pixelRatio = window.devicePixelRatio || 1;
  canvas.width = Math.round(width * pixelRatio);
  canvas.height = Math.round(height * pixelRatio);
  const context = canvas.getContext("2d");
  context.scale(pixelRatio, pixelRatio);
  const project = projector(width, height);
  drawFeatures(context, project);
  data.points.forEach((point, index) => {
    const [x, y] = project(point.position);
    buttons[index].style.left = x + "px";
    buttons[index].style.top = y + "px";
  });
}

if (positions.length) new ResizeObserver(render).observe(map);
