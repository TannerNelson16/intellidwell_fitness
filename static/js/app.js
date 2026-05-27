/* app.js - clean HiDPI canvas charts + resize rerender + existing SW/push/progress */

let swRegistrationPromise = null;

async function ensureServiceWorker() {
  if (!("serviceWorker" in navigator)) return null;

  if (!swRegistrationPromise) {
    swRegistrationPromise = navigator.serviceWorker
      .register("/sw.js", { scope: "/" })
      .then(() => navigator.serviceWorker.ready)
      .catch((err) => {
        console.error("Service worker registration failed", err);
        return null;
      });
  }
  return swRegistrationPromise;
}

function debounce(fn, ms = 120) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

/**
 * Makes canvas crisp on HiDPI and returns a ctx configured so you draw in CSS pixels.
 * IMPORTANT: after this, use w/h returned here (NOT ctx.canvas.width/height).
 */
function setupHiDPICanvas(canvas) {
  const dpr = Math.max(window.devicePixelRatio || 1, 1);
  const rect = canvas.getBoundingClientRect();
  const cssW = Math.max(1, Math.round(rect.width));
  const cssH = Math.max(1, Math.round(rect.height));

  const pxW = Math.round(cssW * dpr);
  const pxH = Math.round(cssH * dpr);

  if (canvas.width !== pxW || canvas.height !== pxH) {
    canvas.width = pxW;
    canvas.height = pxH;
  }

  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0); // now draw in CSS px
  ctx.imageSmoothingEnabled = true;

  return { ctx, w: cssW, h: cssH };
}

function niceTicks(min, max, count = 4) {
  const span = Math.max(max - min, 1e-9);
  const step0 = span / count;
  const pow10 = Math.pow(10, Math.floor(Math.log10(step0)));
  const err = step0 / pow10;

  const step =
    err >= 7.5 ? 10 * pow10 :
    err >= 3.5 ? 5 * pow10 :
    err >= 1.5 ? 2 * pow10 : pow10;

  const start = Math.floor(min / step) * step;
  const end = Math.ceil(max / step) * step;

  const ticks = [];
  for (let v = start; v <= end + step * 0.5; v += step) ticks.push(v);
  return ticks;
}

function formatCompactValue(value) {
  if (value >= 1000) return `${(value / 1000).toFixed(value >= 10000 ? 0 : 1)}k`;
  if (value % 1 !== 0) return value.toFixed(1);
  return String(Math.round(value));
}

function drawLegendPill(ctx, x, y, label, color, alpha = 1) {
  ctx.save();
  ctx.font = "12px Inter, system-ui, sans-serif";
  const textW = ctx.measureText(label).width;
  const w = textW + 34;
  const h = 24;
  ctx.fillStyle = "rgba(10, 18, 34, 0.76)";
  roundRectFill(ctx, x, y, w, h, 12);
  ctx.fillStyle = color;
  ctx.globalAlpha = alpha;
  roundRectFill(ctx, x + 8, y + 7, 10, 10, 4);
  ctx.globalAlpha = 1;
  ctx.fillStyle = "#d6e2ff";
  ctx.fillText(label, x + 24, y + 16);
  ctx.restore();
}

function urlBase64ToUint8Array(base64String) {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const rawData = atob(base64);
  const outputArray = new Uint8Array(rawData.length);
  for (let i = 0; i < rawData.length; ++i) outputArray[i] = rawData.charCodeAt(i);
  return outputArray;
}

/* ----------------------- Chart data + render manager ----------------------- */

let __chartData = {
  metrics: null,
  weekly: null,
  compareWeight: null,
  compareMacro: null,
};

let __chartTooltipsBound = false;

function renderAllCharts() {
  // Metrics (trend)
  const metricsCanvas = document.getElementById("metricsChart");
  if (metricsCanvas && __chartData.metrics) {
    const { ctx, w, h } = setupHiDPICanvas(metricsCanvas);
    renderTrendChart(ctx, w, h, __chartData.metrics);
  }

  // Weekly bars
  const weeklyCanvas = document.getElementById("weeklyChart");
  if (weeklyCanvas && __chartData.weekly) {
    const { ctx, w, h } = setupHiDPICanvas(weeklyCanvas);
    renderWeeklyBar(ctx, w, h, __chartData.weekly);
  }

  // Compare weight
  const compareWeightCanvas = document.getElementById("compareWeightChart");
  if (compareWeightCanvas && __chartData.compareWeight) {
    const { ctx, w, h } = setupHiDPICanvas(compareWeightCanvas);
    renderCompareWeight(ctx, w, h, __chartData.compareWeight);
  }

  // Compare macros
  const compareMacroCanvas = document.getElementById("compareMacroChart");
  if (compareMacroCanvas && __chartData.compareMacro) {
    const { ctx, w, h } = setupHiDPICanvas(compareMacroCanvas);
    renderCompareMacro(ctx, w, h, __chartData.compareMacro);
  }
}

/* ---------------------------- DOMContentLoaded ---------------------------- */

document.addEventListener("DOMContentLoaded", async () => {
  // SW registration (non-blocking)
  ensureServiceWorker();

  // Progress rings
  document.querySelectorAll(".progress-ring").forEach((ring) => {
    const target = parseFloat(ring.dataset.progress || "0");
    const inner = ring.querySelector(".progress-inner");
    let current = 0;

    const animate = () => {
      current += (target - current) * 0.08;
      if (Math.abs(current - target) < 0.001) current = target;
      else requestAnimationFrame(animate);

      ring.style.setProperty("--progress", current);
      if (inner) inner.setAttribute("aria-valuenow", Math.round(current * 100));
    };

    requestAnimationFrame(animate);
  });

  // Push notifications
  const enableBtn = document.getElementById("enablePushBtn");
  const vapidKey = document.body.dataset.vapid || "";

  if (enableBtn && vapidKey) {
    const serverEnabled = enableBtn.dataset.enabled === "true";

    const setButtonState = (subscribed) => {
      if (subscribed) {
        enableBtn.textContent = "Notifications enabled";
        enableBtn.disabled = true;
        enableBtn.dataset.enabled = "true";
      } else {
        enableBtn.textContent = "Enable notifications";
        enableBtn.disabled = false;
        enableBtn.dataset.enabled = "false";
      }
    };

    setButtonState(serverEnabled);

    ensureServiceWorker().then(async (registration) => {
      if (!registration) return;
      const existing = await registration.pushManager.getSubscription();
      if (serverEnabled) setButtonState(!!existing);
      else if (existing) setButtonState(false);
    });

    enableBtn.addEventListener("click", async () => {
      if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
        alert("Push notifications are not supported in this browser.");
        return;
      }

      const permission = await Notification.requestPermission();
      if (permission !== "granted") {
        alert("Notification permission denied.");
        return;
      }

      try {
        const registration = await ensureServiceWorker();
        if (!registration) {
          alert("Service worker unavailable.");
          return;
        }

        const existing = await registration.pushManager.getSubscription();
        if (existing) await existing.unsubscribe();

        const subscription = await registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(vapidKey),
        });

        await fetch("/notifications/subscribe", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ subscription }),
        });

        setButtonState(true);
      } catch (err) {
        console.error(err);
        alert("Unable to enable notifications.");
        setButtonState(false);
      }
    });
  }

  // Capture chart datasets once
  try {
    const metricsCanvas = document.getElementById("metricsChart");
    if (metricsCanvas?.dataset.metrics) __chartData.metrics = JSON.parse(metricsCanvas.dataset.metrics);

    const weeklyCanvas = document.getElementById("weeklyChart");
    if (weeklyCanvas?.dataset.weekly) __chartData.weekly = JSON.parse(weeklyCanvas.dataset.weekly);

    const compareWeightCanvas = document.getElementById("compareWeightChart");
    if (compareWeightCanvas?.dataset.compareWeight) __chartData.compareWeight = JSON.parse(compareWeightCanvas.dataset.compareWeight);

    const compareMacroCanvas = document.getElementById("compareMacroChart");
    if (compareMacroCanvas?.dataset.compareMacro) __chartData.compareMacro = JSON.parse(compareMacroCanvas.dataset.compareMacro);
  } catch (err) {
    console.error("Unable to parse chart datasets", err);
  }

  // Render after layout is stable
  requestAnimationFrame(() => renderAllCharts());

  if (!__chartTooltipsBound) {
    bindChartTooltip("metricsChart", (canvas, event) => buildTrendTooltip(canvas, event, __chartData.metrics));
    bindChartTooltip("weeklyChart", (canvas, event) => buildWeeklyTooltip(canvas, event, __chartData.weekly));
    __chartTooltipsBound = true;
  }

  // Re-render on resize
  window.addEventListener("resize", debounce(renderAllCharts, 150));
});

/* ------------------------------- Chart code ------------------------------- */

function renderTrendChart(ctx, w, h, data) {
  const { labels = [], weight = [], bodyFat = [] } = data;
  ctx.clearRect(0, 0, w, h);

  drawChartBackdrop(ctx, w, h);

  const padL = 52, padR = 52, padT = 28, padB = 40;
  const plotW = Math.max(10, w - padL - padR);
  const plotH = Math.max(10, h - padT - padB);

  const wVals = weight.map(Number).filter((v) => Number.isFinite(v));
  const bVals = bodyFat.map(Number).filter((v) => Number.isFinite(v));

  if (!wVals.length && !bVals.length) {
    ctx.fillStyle = "#9db2ce";
    ctx.textAlign = "center";
    ctx.font = "13px Inter, system-ui, sans-serif";
    ctx.fillText("Start logging weight/body fat to unlock this chart.", w / 2, h / 2);
    return;
  }

  const wMin = wVals.length ? Math.min(...wVals) : 0;
  const wMax = wVals.length ? Math.max(...wVals) : 1;
  const bMin = bVals.length ? Math.min(...bVals) : 0;
  const bMax = bVals.length ? Math.max(...bVals) : 1;

  const wSpan = Math.max(wMax - wMin, 1);
  const bSpan = Math.max(bMax - bMin, 1);

  const wLo = wMin - wSpan * 0.12;
  const wHi = wMax + wSpan * 0.10;
  const bLo = bMin - bSpan * 0.12;
  const bHi = bMax + bSpan * 0.10;

  const xAt = (i) =>
    padL + (labels.length <= 1 ? plotW / 2 : (i / (labels.length - 1)) * plotW);

  const yW = (v) => padT + (1 - (v - wLo) / (wHi - wLo || 1)) * plotH;
  const yB = (v) => padT + (1 - (v - bLo) / (bHi - bLo || 1)) * plotH;

  // grid (weight ticks)
  ctx.strokeStyle = "rgba(255,255,255,0.05)";
  ctx.lineWidth = 1;
  const yTicksW = niceTicks(wLo, wHi, 4);
  yTicksW.forEach((t) => {
    const y = yW(t);
    ctx.beginPath();
    ctx.moveTo(padL, y);
    ctx.lineTo(padL + plotW, y);
    ctx.stroke();
  });

  // frame
  ctx.strokeStyle = "rgba(255,255,255,0.10)";
  roundRectStroke(ctx, padL, padT, plotW, plotH, 18);

  // y labels (left weight)
  ctx.font = "11px Inter, system-ui, sans-serif";
  ctx.fillStyle = "#9db2ce";
  ctx.textAlign = "right";
  yTicksW.forEach((t) => ctx.fillText(formatCompactValue(t), padL - 10, yW(t) + 4));

  // y labels (right body fat)
  const yTicksB = niceTicks(bLo, bHi, 4);
  ctx.textAlign = "left";
  yTicksB.forEach((t) => ctx.fillText(`${t.toFixed(1)}%`, padL + plotW + 10, yB(t) + 4));

  // axis titles
  ctx.save();
  ctx.fillStyle = "rgba(214, 226, 255, 0.62)";
  ctx.font = "11px Inter, system-ui, sans-serif";
  ctx.textAlign = "left";
  ctx.fillText("Weight", padL, padT - 12);
  ctx.textAlign = "right";
  ctx.fillText("Body fat", padL + plotW, padT - 12);
  ctx.restore();

  // x labels (sparser)
  ctx.textAlign = "center";
  const skip = labels.length > 6 ? 2 : 1;
  labels.forEach((lab, i) => {
    if (i % skip !== 0 && i !== labels.length - 1) return;
    ctx.fillText(lab, xAt(i), h - 10);
  });

  function drawSeries(values, yFn, stroke, fill) {
    const pts = values
      .map((v, i) => ({ v: Number(v), i }))
      .filter((p) => Number.isFinite(p.v));

    if (!pts.length) return;

    ctx.lineWidth = 3;
    ctx.lineCap = "round";
    ctx.strokeStyle = stroke;

    ctx.beginPath();
    pts.forEach((p, idx) => {
      const x = xAt(p.i);
      const y = yFn(p.v);
      if (idx === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();

    if (fill && pts.length > 1) {
      ctx.save();
      ctx.beginPath();
      pts.forEach((p, idx) => {
        const x = xAt(p.i);
        const y = yFn(p.v);
        if (idx === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.lineTo(xAt(pts[pts.length - 1].i), padT + plotH);
      ctx.lineTo(xAt(pts[0].i), padT + plotH);
      ctx.closePath();
      ctx.fillStyle = fill;
      ctx.fill();
      ctx.restore();
    }

    ctx.fillStyle = stroke;
    pts.forEach((p) => {
      ctx.beginPath();
      ctx.arc(xAt(p.i), yFn(p.v), 3, 0, Math.PI * 2);
      ctx.fill();
    });
  }

  drawSeries(weight, yW, "#76d0ff", "rgba(118, 208, 255, 0.12)");
  drawSeries(bodyFat, yB, "#63f0c7", "rgba(99, 240, 199, 0.10)");

  // legend
  drawLegendPill(ctx, padL, 10, "Weight (lb)", "#76d0ff");
  drawLegendPill(ctx, padL + 130, 10, "Body fat (%)", "#63f0c7");
}

function renderWeeklyBar(ctx, w, h, dataset) {
  const selfPoints = dataset.self || [];
  const partnerPoints = dataset.partner || [];
  const labels = selfPoints.length ? selfPoints.map((p) => p.label) : partnerPoints.map((p) => p.label);

  ctx.clearRect(0, 0, w, h);
  drawChartBackdrop(ctx, w, h);

  if (!labels.length) {
    ctx.fillStyle = "#9db2ce";
    ctx.textAlign = "center";
    ctx.font = "13px Inter, system-ui, sans-serif";
    ctx.fillText("Log at least one day to see weekly bars.", w / 2, h / 2);
    return;
  }

  const series = [
    { key: "calories", color: "#7ee0ff" },
    { key: "protein", color: "#7cffa7" },
    { key: "water", color: "#8fa8ff" },
  ];

  const padding = 38;
  const barWidth = 12;
  const setGap = 7;
  const groupGap = 20;

  const plotW = Math.max(10, w - padding * 2);
  const plotH = Math.max(10, h - padding * 2);

  const allVals = [];
  [...selfPoints, ...partnerPoints].forEach((p) => {
    series.forEach((s) => allVals.push((p && p[s.key]) || 0));
  });
  const maxVal = Math.max(...allVals, 1);
  const yTicks = niceTicks(0, maxVal, 4);

  const yAt = (v) => padding + (1 - v / maxVal) * plotH;

  yTicks.forEach((tick) => {
    const y = yAt(tick);
    ctx.strokeStyle = "rgba(255,255,255,0.05)";
    ctx.beginPath();
    ctx.moveTo(padding, y);
    ctx.lineTo(padding + plotW, y);
    ctx.stroke();

    ctx.fillStyle = "#9db2ce";
    ctx.textAlign = "right";
    ctx.font = "11px Inter, system-ui, sans-serif";
    ctx.fillText(formatCompactValue(tick), padding - 10, y + 4);
  });

  // frame
  ctx.strokeStyle = "rgba(255,255,255,0.10)";
  roundRectStroke(ctx, padding, padding, plotW, plotH, 18);

  // bar layout
  const perDayBars = partnerPoints.length ? 6 : 3;
  const dayBlockW = perDayBars * barWidth + (perDayBars - 1) * setGap;
  const totalW = labels.length * dayBlockW + (labels.length - 1) * groupGap;

  // center if there is extra space
  const startX = padding + Math.max(0, (plotW - totalW) / 2);

  labels.forEach((label, idx) => {
    const baseX = startX + idx * (dayBlockW + groupGap);

    // self
    series.forEach((s, i) => {
      const v = (selfPoints[idx] && selfPoints[idx][s.key]) || 0;
      const x = baseX + i * (barWidth + setGap);
      const y = yAt(v);
      ctx.fillStyle = s.color;
      drawRoundedBar(ctx, x, y, barWidth, padding + plotH - y, 4);
    });

    // partner
    if (partnerPoints.length) {
      series.forEach((s, i) => {
        const v = (partnerPoints[idx] && partnerPoints[idx][s.key]) || 0;
        const x = baseX + 3 * (barWidth + setGap) + setGap + i * (barWidth + setGap);
        const y = yAt(v);
        ctx.fillStyle = `${s.color}99`;
        drawRoundedBar(ctx, x, y, barWidth, padding + plotH - y, 4);
      });
    }

    // x labels
    ctx.fillStyle = "#9db2ce";
    ctx.textAlign = "center";
    ctx.font = "11px Inter, system-ui, sans-serif";
    ctx.fillText(label, baseX + dayBlockW / 2, padding + plotH + 16);
  });

  // legend
  drawLegendPill(ctx, padding, 10, "You", "#7ee0ff");
  if (partnerPoints.length) {
    drawLegendPill(ctx, padding + 86, 10, "Partner", "#7ee0ff", 0.6);
  }
}

function renderCompareWeight(ctx, w, h, data) {
  if (!data || !data.labels) return;
  const { labels, self = [], partner = [] } = data;

  ctx.clearRect(0, 0, w, h);

  const padding = 28;
  const plotW = Math.max(10, w - padding * 2);
  const plotH = Math.max(10, h - padding * 2);

  const combined = [...self.filter((v) => v != null), ...partner.filter((v) => v != null)];
  if (!combined.length) {
    ctx.fillStyle = "#9db2ce";
    ctx.textAlign = "center";
    ctx.font = "13px Inter, system-ui, sans-serif";
    ctx.fillText("Log weight to see the trend.", w / 2, h / 2);
    return;
  }

  const rawMin = Math.min(...combined);
  const rawMax = Math.max(...combined);
  const span = Math.max(rawMax - rawMin, 1);
  const minVal = rawMin - span * 0.1;
  const maxVal = rawMax + span * 0.06;

  const xAt = (idx) =>
    padding + (labels.length <= 1 ? plotW / 2 : (idx / (labels.length - 1)) * plotW);
  const yAt = (v) =>
    padding + (1 - (v - minVal) / (maxVal - minVal || 1)) * plotH;

  // frame
  ctx.strokeStyle = "rgba(255,255,255,0.10)";
  ctx.strokeRect(padding, padding, plotW, plotH);

  // x labels
  ctx.font = "11px Inter, system-ui, sans-serif";
  ctx.fillStyle = "#9db2ce";
  labels.forEach((label, idx) => {
    const x = xAt(idx);
    ctx.fillText(label, x - ctx.measureText(label).width / 2, h - 8);
  });

  const drawLine = (values, color) => {
    const pts = values.map((v, idx) => ({ v, idx })).filter((p) => p.v != null);
    if (!pts.length) return;

    ctx.strokeStyle = color;
    ctx.lineWidth = 2.4;
    ctx.lineCap = "round";

    ctx.beginPath();
    pts.forEach(({ v, idx }, i) => {
      const x = xAt(idx);
      const y = yAt(v);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  };

  drawLine(self, "#7ee0ff");
  drawLine(partner, "#7cffa7");

  // legend
  ctx.fillStyle = "#7ee0ff";
  ctx.fillRect(padding, padding - 16, 12, 12);
  ctx.fillStyle = "#d6e2ff";
  ctx.font = "12px Inter, system-ui, sans-serif";
  ctx.fillText("You", padding + 18, padding - 5);

  if (partner.some((v) => v != null)) {
    ctx.fillStyle = "#7cffa7";
    ctx.fillRect(padding + 70, padding - 16, 12, 12);
    ctx.fillStyle = "#d6e2ff";
    ctx.fillText("Partner", padding + 88, padding - 5);
  }
}

function renderCompareMacro(ctx, w, h, data) {
  if (!data || !data.labels) return;

  const {
    labels,
    calories_self = [],
    calories_partner = [],
    protein_self = [],
    protein_partner = [],
    water_self = [],
    water_partner = [],
  } = data;

  ctx.clearRect(0, 0, w, h);

  const padding = 30;
  const plotW = Math.max(10, w - padding * 2);
  const plotH = Math.max(10, h - padding * 2);

  const barWidth = 8;
  const setGap = 4;
  const groupGap = 12;

  const allVals = [
    ...calories_self,
    ...calories_partner,
    ...protein_self,
    ...protein_partner,
    ...water_self,
    ...water_partner,
  ].map((v) => v || 0);

  const maxVal = Math.max(...allVals, 1);
  const yAt = (v) => padding + (1 - v / maxVal) * plotH;

  // frame
  ctx.strokeStyle = "rgba(255,255,255,0.10)";
  ctx.strokeRect(padding, padding, plotW, plotH);

  const perDayBars = 6;
  const dayBlockW = perDayBars * barWidth + (perDayBars - 1) * setGap;
  const totalW = labels.length * dayBlockW + (labels.length - 1) * groupGap;
  const startX = padding + Math.max(0, (plotW - totalW) / 2);

  labels.forEach((label, idx) => {
    const baseX = startX + idx * (dayBlockW + groupGap);

    const bars = [
      { v: calories_self[idx] || 0, color: "#7ee0ff" },
      { v: protein_self[idx] || 0, color: "#7cffa7" },
      { v: water_self[idx] || 0, color: "#8fa8ff" },
      { v: calories_partner[idx] || 0, color: "#7ee0ff99" },
      { v: protein_partner[idx] || 0, color: "#7cffa799" },
      { v: water_partner[idx] || 0, color: "#8fa8ff99" },
    ];

    bars.forEach((b, i) => {
      const x = baseX + i * (barWidth + setGap);
      const y = yAt(b.v);
      ctx.fillStyle = b.color;
      drawRoundedBar(ctx, x, y, barWidth, padding + plotH - y, 4);
    });

    // x label
    ctx.fillStyle = "#9db2ce";
    ctx.textAlign = "center";
    ctx.font = "11px Inter, system-ui, sans-serif";
    ctx.fillText(label, baseX + dayBlockW / 2, padding + plotH + 16);
  });

  // legend (2 rows of 3)
  const legendItems = [
    { label: "You cal", color: "#7ee0ff" },
    { label: "You protein", color: "#7cffa7" },
    { label: "You water", color: "#8fa8ff" },
    { label: "Partner cal", color: "#7ee0ff99" },
    { label: "Partner protein", color: "#7cffa799" },
    { label: "Partner water", color: "#8fa8ff99" },
  ];

  ctx.textAlign = "left";
  ctx.font = "11px Inter, system-ui, sans-serif";
  legendItems.forEach((item, idx) => {
    const x = padding + (idx % 3) * 140;
    const y = padding - 18 + Math.floor(idx / 3) * 16;
    ctx.fillStyle = item.color;
    ctx.fillRect(x, y, 10, 10);
    ctx.fillStyle = "#d6e2ff";
    ctx.fillText(item.label, x + 14, y + 9);
  });
}

function drawRoundedBar(ctx, x, y, w, h, r) {
  const radius = Math.min(r, w / 2, h);
  ctx.beginPath();
  ctx.moveTo(x, y + h);
  ctx.lineTo(x, y + radius);
  ctx.quadraticCurveTo(x, y, x + radius, y);
  ctx.lineTo(x + w - radius, y);
  ctx.quadraticCurveTo(x + w, y, x + w, y + radius);
  ctx.lineTo(x + w, y + h);
  ctx.closePath();
  ctx.fill();
}

function roundRectFill(ctx, x, y, w, h, r) {
  const radius = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + radius, y);
  ctx.lineTo(x + w - radius, y);
  ctx.quadraticCurveTo(x + w, y, x + w, y + radius);
  ctx.lineTo(x + w, y + h - radius);
  ctx.quadraticCurveTo(x + w, y + h, x + w - radius, y + h);
  ctx.lineTo(x + radius, y + h);
  ctx.quadraticCurveTo(x, y + h, x, y + h - radius);
  ctx.lineTo(x, y + radius);
  ctx.quadraticCurveTo(x, y, x + radius, y);
  ctx.closePath();
  ctx.fill();
}

function roundRectStroke(ctx, x, y, w, h, r) {
  const radius = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + radius, y);
  ctx.lineTo(x + w - radius, y);
  ctx.quadraticCurveTo(x + w, y, x + w, y + radius);
  ctx.lineTo(x + w, y + h - radius);
  ctx.quadraticCurveTo(x + w, y + h, x + w - radius, y + h);
  ctx.lineTo(x + radius, y + h);
  ctx.quadraticCurveTo(x, y + h, x, y + h - radius);
  ctx.lineTo(x, y + radius);
  ctx.quadraticCurveTo(x, y, x + radius, y);
  ctx.stroke();
}

function drawChartBackdrop(ctx, w, h) {
  const bg = ctx.createLinearGradient(0, 0, 0, h);
  bg.addColorStop(0, 'rgba(255,255,255,0.028)');
  bg.addColorStop(1, 'rgba(255,255,255,0.01)');
  ctx.fillStyle = bg;
  ctx.fillRect(0, 0, w, h);

  ctx.save();
  ctx.strokeStyle = 'rgba(255,255,255,0.025)';
  ctx.lineWidth = 1;
  for (let y = 24; y < h; y += 28) {
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(w, y);
    ctx.stroke();
  }
  ctx.restore();
}

function ensureChartTooltip() {
  let tooltip = document.getElementById('chartTooltip');
  if (!tooltip) {
    tooltip = document.createElement('div');
    tooltip.id = 'chartTooltip';
    tooltip.style.position = 'fixed';
    tooltip.style.zIndex = '9999';
    tooltip.style.pointerEvents = 'none';
    tooltip.style.padding = '10px 12px';
    tooltip.style.borderRadius = '12px';
    tooltip.style.background = 'rgba(9, 15, 28, 0.94)';
    tooltip.style.border = '1px solid rgba(255,255,255,0.10)';
    tooltip.style.color = '#f7fbff';
    tooltip.style.font = '12px Inter, system-ui, sans-serif';
    tooltip.style.boxShadow = '0 14px 40px rgba(0,0,0,0.35)';
    tooltip.style.backdropFilter = 'blur(10px)';
    tooltip.style.display = 'none';
    document.body.appendChild(tooltip);
  }
  return tooltip;
}

function showChartTooltip(x, y, html) {
  const tooltip = ensureChartTooltip();
  tooltip.innerHTML = html;
  tooltip.style.left = `${x + 14}px`;
  tooltip.style.top = `${y + 14}px`;
  tooltip.style.display = 'block';
}

function hideChartTooltip() {
  const tooltip = document.getElementById('chartTooltip');
  if (tooltip) tooltip.style.display = 'none';
}

function bindChartTooltip(canvasId, resolver) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;

  canvas.addEventListener('mousemove', (event) => {
    const payload = resolver(canvas, event);
    if (!payload) {
      hideChartTooltip();
      return;
    }
    showChartTooltip(event.clientX, event.clientY, payload);
  });

  canvas.addEventListener('mouseleave', hideChartTooltip);
}

function buildTrendTooltip(canvas, event, data) {
  if (!data?.labels?.length) return null;
  const rect = canvas.getBoundingClientRect();
  const x = event.clientX - rect.left;
  const padL = 52;
  const padR = 52;
  const plotW = Math.max(10, rect.width - padL - padR);
  const ratio = Math.min(Math.max((x - padL) / plotW, 0), 1);
  const idx = Math.round(ratio * (data.labels.length - 1));
  if (idx < 0 || idx >= data.labels.length) return null;

  const weight = data.weight?.[idx];
  const bodyFat = data.bodyFat?.[idx];
  return `
    <strong>${data.labels[idx]}</strong><br>
    Weight: ${weight ?? '—'}<br>
    Body fat: ${bodyFat != null ? `${bodyFat}%` : '—'}
  `;
}

function buildWeeklyTooltip(canvas, event, data) {
  const selfPoints = data?.self || [];
  if (!selfPoints.length) return null;
  const rect = canvas.getBoundingClientRect();
  const x = event.clientX - rect.left;
  const padding = 38;
  const plotW = Math.max(10, rect.width - padding * 2);
  const dayCount = selfPoints.length;
  const ratio = Math.min(Math.max((x - padding) / plotW, 0), 1);
  const idx = Math.round(ratio * (dayCount - 1));
  if (idx < 0 || idx >= dayCount) return null;

  const point = selfPoints[idx];
  const partner = (data.partner || [])[idx];
  return `
    <strong>${point.label}</strong><br>
    Calories: ${point.calories}<br>
    Protein: ${point.protein}g<br>
    Water: ${point.water}oz
    ${partner ? `<br><span style="color:#9db2ce">Partner: ${partner.calories} cal, ${partner.protein}g, ${partner.water}oz</span>` : ''}
  `;
}
