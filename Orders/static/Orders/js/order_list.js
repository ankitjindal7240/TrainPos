document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".expand-button").forEach((button) => {
    button.addEventListener("click", () => {
      const detailRow = document.getElementById(button.dataset.detailId);
      const isExpanded = button.getAttribute("aria-expanded") === "true";

      button.setAttribute("aria-expanded", String(!isExpanded));
      detailRow.hidden = isExpanded;
    });
  });
});
