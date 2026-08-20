window.addEventListener("load", () => {
  window.print();
});

window.addEventListener("afterprint", () => {
  window.close();
});
