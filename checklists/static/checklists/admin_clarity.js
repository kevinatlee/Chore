document.addEventListener("DOMContentLoaded", () => {
  const form = document.querySelector("#content-main form");
  if (!form) return;

  const activeField = form.querySelector("#id_is_active");
  const wasActive = activeField?.checked;
  const allowNaField = form.querySelector("#id_allow_na");
  const allowedNa = allowNaField?.checked;

  form.addEventListener("submit", event => {
    if (wasActive && activeField && !activeField.checked) {
      const confirmed = window.confirm(
        "Deactivate this configuration for future use? Historical Chore Lists, Staff Contributions, and reports will be preserved."
      );
      if (!confirmed) {
        event.preventDefault();
        return;
      }
    }
    if (allowedNa && allowNaField && !allowNaField.checked) {
      const confirmed = window.confirm(
        "Remove N/A from future Chore Lists for this task? Existing Chore List snapshots will keep their current N/A setting."
      );
      if (!confirmed) event.preventDefault();
    }
  });
});
