(() => {
  const form = document.getElementById("checklist-selector");
  const date = document.getElementById("date");
  const position = document.getElementById("category");
  const shift = document.getElementById("shift");
  const choicesElement = document.getElementById("position-shift-options");
  if (!form || !date || !position || !shift || !choicesElement) return;

  const choices = JSON.parse(choicesElement.textContent);
  const updateShiftChoices = () => {
    const previousShift = shift.value;
    shift.replaceChildren(
      ...(choices[position.value] || []).map(
        choice => new Option(
          choice.name,
          choice.id,
          false,
          String(choice.id) === previousShift,
        )
      )
    );
  };

  const refreshDashboard = () => {
    if (!date.value) return;
    const params = new URLSearchParams({ date: date.value });
    ["staff", "category", "shift", "program"].forEach(name => {
      const control = form.elements.namedItem(name);
      if (control?.value) params.set(name, control.value);
    });
    window.location.assign(`${form.dataset.dashboardUrl}?${params.toString()}`);
  };

  position.addEventListener("change", updateShiftChoices);
  date.addEventListener("change", refreshDashboard);
})();
