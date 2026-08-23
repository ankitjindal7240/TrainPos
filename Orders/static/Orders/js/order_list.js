document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".expand-button").forEach((button) => {
    button.addEventListener("click", () => {
      const detailRow = document.getElementById(button.dataset.detailId);
      const isExpanded = button.getAttribute("aria-expanded") === "true";

      button.setAttribute("aria-expanded", String(!isExpanded));
      detailRow.hidden = isExpanded;
    });
  });

  const activeRefreshes = new Set();
  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content;

  document.querySelectorAll(".live-refresh").forEach((button) => {
    button.addEventListener("click", async () => {
      const runKey = button.dataset.trainRun;
      if (activeRefreshes.has(runKey)) {
        return;
      }

      activeRefreshes.add(runKey);
      setRunButtonsDisabled(runKey, true);
      clearRefreshFailure(runKey);
      try {
        const response = await fetch(button.dataset.refreshUrl, {
          method: "POST",
          headers: {
            "Content-Type": "application/x-www-form-urlencoded",
            "X-CSRFToken": csrfToken,
          },
          body: new URLSearchParams({ journey_date: button.dataset.journeyDate }),
        });
        const payload = await response.json();
        if (!response.ok || !payload.ok) {
          throw new Error(payload.error || "Refresh failed.");
        }
        updateRunStatus(runKey, payload.status);
        markRefreshSuccess(runKey);
      } catch (error) {
        markRefreshFailure(runKey);
      } finally {
        activeRefreshes.delete(runKey);
        setRunButtonsDisabled(runKey, false);
      }
    });
  });

  function updateRunStatus(runKey, status) {
    document.querySelectorAll("[data-live-eta]").forEach((cell) => {
      if (cell.dataset.trainRun !== runKey) return;
      const etaValue = cell.querySelector("[data-eta-value]");
      const etaLabel = cell.querySelector("[data-eta-label]");
      if (status.display_state === "LIVE") {
        etaValue.textContent = status.expected_arrival || "—";
        if (etaLabel) etaLabel.remove();
      } else if (status.display_state === "NOT_STARTED") {
        etaValue.textContent = status.scheduled_arrival || "—";
        if (!etaLabel) {
          const label = document.createElement("small");
          label.dataset.etaLabel = "";
          cell.insertBefore(label, cell.querySelector("[data-updated-label]"));
        }
        cell.querySelector("[data-eta-label]").textContent = "Scheduled";
      } else {
        etaValue.textContent = "Reached";
        if (etaLabel) etaLabel.remove();
      }
      cell.querySelector("[data-updated-label]").textContent = status.updated_label || "Updated just now";
    });
    document.querySelectorAll("[data-live-delay]").forEach((cell) => {
      if (cell.dataset.trainRun !== runKey) return;
      const value = cell.querySelector("[data-delay-value]");
      value.textContent = status.display_state === "LIVE" && status.delay_minutes !== null
        ? (status.delay_minutes ? `+${status.delay_minutes} min` : "On time")
        : "—";
      cell.classList.toggle("delayed", status.delay_minutes > 0);
    });
    document.querySelectorAll("[data-live-arriving]").forEach((cell) => {
      if (cell.dataset.trainRun !== runKey) return;
      const value = cell.querySelector("[data-arriving-value]");
      value.textContent = status.display_state === "LIVE"
        ? (status.arriving_in || "—")
        : (status.display_state === "REACHED" ? "Reached" : "—");
      cell.className = `live-arriving urgency-${status.urgency.toLowerCase()}`;
    });
    document.querySelectorAll(".live-refresh").forEach((button) => {
      if (button.dataset.trainRun === runKey) {
        button.dataset.journeyDate = status.journey_date;
      }
    });
    document.querySelectorAll(".order-live-indicator").forEach((indicator) => {
      if (indicator.dataset.trainRun !== runKey) return;
      indicator.className = `order-live-indicator urgency-${status.urgency.toLowerCase()}`;
      indicator.textContent = status.display_state === "LIVE" ? "LIVE" : status.display_state === "NOT_STARTED" ? "NOT STARTED" : "REACHED";
    });
  }

  function setRunButtonsDisabled(runKey, disabled) {
    document.querySelectorAll(".live-refresh").forEach((button) => {
      if (button.dataset.trainRun === runKey) button.disabled = disabled;
      if (button.dataset.trainRun === runKey) {
        button.classList.toggle("is-loading", disabled);
        button.setAttribute("aria-busy", String(disabled));
      }
    });
  }

  function clearRefreshFailure(runKey) {
    document.querySelectorAll(".live-refresh").forEach((button) => {
      if (button.dataset.trainRun === runKey) {
        button.classList.remove("refresh-failed");
        button.title = "Refresh live train status";
        const result = button.parentElement.querySelector("[data-refresh-result]");
        result.textContent = "";
        result.classList.remove("refresh-failed");
      }
    });
  }

  function markRefreshFailure(runKey) {
    document.querySelectorAll(".live-refresh").forEach((button) => {
      if (button.dataset.trainRun === runKey) {
        button.classList.add("refresh-failed");
        button.title = "Refresh failed";
        const result = button.parentElement.querySelector("[data-refresh-result]");
        result.textContent = "Refresh failed";
        result.classList.add("refresh-failed");
      }
    });
  }

  function markRefreshSuccess(runKey) {
    document.querySelectorAll(".live-refresh").forEach((button) => {
      if (button.dataset.trainRun !== runKey) return;
      const result = button.parentElement.querySelector("[data-refresh-result]");
      result.textContent = "✓ Updated";
      result.classList.remove("refresh-failed");
      window.setTimeout(() => {
        if (result.textContent === "✓ Updated") result.textContent = "";
      }, 3000);
    });
  }

  const dashboardPoll = document.getElementById("order-dashboard-poll");
  if (!dashboardPoll) return;

  let knownDashboardToken = dashboardPoll.dataset.token;
  let dashboardCheckInProgress = false;
  let reloadScheduled = false;

  async function checkForNewOrders() {
    if (document.visibilityState === "hidden" || dashboardCheckInProgress || reloadScheduled) {
      return;
    }
    dashboardCheckInProgress = true;
    try {
      const response = await fetch(dashboardPoll.dataset.url, {
        headers: { Accept: "application/json" },
      });
      if (!response.ok) return;
      const version = await response.json();
      if (version.token !== knownDashboardToken) {
        reloadScheduled = true;
        const notice = document.getElementById("new-order-notice");
        notice.hidden = false;
        window.setTimeout(() => window.location.reload(), 650);
        return;
      }
      knownDashboardToken = version.token;
    } catch (error) {
      // A failed check is intentionally silent; the next interval will retry.
    } finally {
      dashboardCheckInProgress = false;
    }
  }

  window.setInterval(checkForNewOrders, 30000);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") checkForNewOrders();
  });
});
