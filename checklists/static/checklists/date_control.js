(() => {
  const formatLocalDate = value => {
    const parts = value.split("-").map(Number);
    if (parts.length !== 3 || parts.some(part => !Number.isInteger(part))) return value;
    const [year, month, day] = parts;
    const localDate = new Date(year, month - 1, day);
    return new Intl.DateTimeFormat(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
    }).format(localDate);
  };

  const initializeDateControl = control => {
    const input = control.querySelector('input[type="date"]');
    const display = control.querySelector("[data-date-control-display]");
    if (!input || !display) return;
    const updateDisplay = () => {
      display.textContent = input.value ? formatLocalDate(input.value) : "Select date";
    };
    input.addEventListener("input", updateDisplay);
    input.addEventListener("change", updateDisplay);
    updateDisplay();
  };

  document.querySelectorAll("[data-date-control]").forEach(initializeDateControl);
})();
