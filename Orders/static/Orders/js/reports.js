document.addEventListener("DOMContentLoaded", () => {
  const period = document.querySelector("#period");
  const customDates = document.querySelector(".custom-date-fields");

  period.addEventListener("change", () => {
    customDates.hidden = period.value !== "custom";
  });
});
