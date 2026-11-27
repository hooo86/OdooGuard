// OdooGuard - formulaire de planification : seuls les champs du mode choisi restent actifs.
(function () {
  function apply(form) {
    var checked = form.querySelector('input[name="mode"]:checked');
    var mode = checked ? checked.value : "";
    form.querySelectorAll(".mode-block").forEach(function (block) {
      var on = block.dataset.mode === mode;
      block.classList.toggle("is-active", on);
      block.classList.toggle("is-off", !on);
      block.querySelectorAll(".mode-fields input, .mode-fields select, .mode-fields textarea")
        .forEach(function (el) { el.disabled = !on; });
    });
    var days = form.querySelector("fieldset[data-days]");
    if (days) {
      var useDays = mode === "times" || mode === "hourly";
      days.disabled = !useDays;
      days.classList.toggle("is-off", !useDays);
    }
  }
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("form[data-schedule-form]").forEach(function (form) {
      form.addEventListener("change", function (e) { if (e.target.name === "mode") apply(form); });
      apply(form);
    });
  });
})();
