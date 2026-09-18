(() => {
  const checklist = document.getElementById("shared-checklist");
  if (!checklist || !window.fetch) return;

  const pollIntervalMs = 7000;
  const syncStatus = document.getElementById("sync-status");
  const resolvedCount = document.getElementById("resolved-count");
  const totalCount = document.getElementById("total-count");
  const contributionList = document.getElementById("recent-contributions");
  let revision = checklist.dataset.revision;
  let requestInFlight = false;
  let activeMutations = 0;

  const stateLabels = { pending: "Pending", completed: "Completed", na: "N/A" };

  const setSyncStatus = (message, kind = "") => {
    syncStatus.textContent = message;
    syncStatus.className = `sync-status ${kind}`.trim();
  };

  const makeButton = (label, state, className) => {
    const button = document.createElement("button");
    button.type = "submit";
    button.name = "state";
    button.value = state;
    button.className = `button ${className}`;
    button.textContent = label;
    return button;
  };

  const refreshActions = (card, state) => {
    const form = card.querySelector("[data-item-form]");
    if (!form) return;
    form.querySelectorAll("button").forEach(button => button.remove());
    if (state !== "completed") form.append(makeButton("Complete", "completed", "complete"));
    if (form.dataset.allowNa === "true" && state !== "na") {
      form.append(makeButton("N/A", "na", "na"));
    }
    if (state !== "pending") form.append(makeButton("Reset", "pending", "reset"));
    if (card.classList.contains("is-saving")) {
      form.querySelectorAll("button").forEach(button => { button.disabled = true; });
    }
  };

  const applyItem = item => {
    const card = checklist.querySelector(`[data-item-id="${item.id}"]`);
    if (!card) return;
    card.classList.remove("state-pending", "state-completed", "state-na", "is-saving");
    card.classList.add(`state-${item.state}`);
    const status = card.querySelector("[data-item-status]");
    status.textContent = item.state_label;
    status.className = `status status-${item.state}`;
    const contributor = card.querySelector("[data-item-contributor]");
    if (contributor) contributor.textContent = item.staff_name
      ? `Staff Contribution by ${item.staff_name}`
      : "No Staff Contribution yet";
    const time = card.querySelector("[data-item-time]");
    if (time) {
      time.hidden = !item.changed_at;
      time.dateTime = item.changed_at || "";
      time.textContent = item.changed_at_label || "";
    }
    refreshActions(card, item.state);
  };

  const refreshContributions = contributions => {
    if (!contributionList) return;
    contributionList.replaceChildren();
    if (!contributions.length) {
      const empty = document.createElement("li");
      empty.textContent = "No Staff Contributions yet.";
      contributionList.append(empty);
      return;
    }
    contributions.forEach(contribution => {
      const row = document.createElement("li");
      const staff = document.createElement("strong");
      staff.textContent = contribution.staff_name;
      const time = document.createElement("time");
      time.dateTime = contribution.created_at;
      time.textContent = contribution.created_at_label;
      row.append(
        staff,
        ` changed “${contribution.task_label}” from ${contribution.previous_state_label} to ${contribution.new_state_label} `,
        time
      );
      contributionList.append(row);
    });
  };

  const applyState = payload => {
    if (!payload.changed) return;
    payload.items.forEach(applyItem);
    refreshContributions(payload.contributions || []);
    resolvedCount.textContent = payload.resolved_count;
    totalCount.textContent = payload.total_count;
    revision = payload.revision;
    checklist.dataset.revision = revision;
  };

  const fetchState = async () => {
    if (requestInFlight || activeMutations || document.hidden) return;
    requestInFlight = true;
    try {
      const separator = checklist.dataset.stateUrl.includes("?") ? "&" : "?";
      const response = await fetch(
        `${checklist.dataset.stateUrl}${separator}revision=${encodeURIComponent(revision)}`,
        { headers: { Accept: "application/json" }, cache: "no-store" }
      );
      if (!response.ok) throw new Error(`Sync failed with ${response.status}`);
      const payload = await response.json();
      applyState(payload);
      setSyncStatus(payload.changed ? "Updated from shared Chore List" : "Up to date");
    } catch (error) {
      setSyncStatus("Unable to sync — retrying", "sync-error");
    } finally {
      requestInFlight = false;
    }
  };

  checklist.addEventListener("submit", async event => {
    const form = event.target.closest("[data-item-form]");
    const submitter = event.submitter;
    if (!form || !submitter || !submitter.value) return;
    event.preventDefault();
    const activity = form.querySelector('[name="activity_text"]');
    if (submitter.value === "completed" && activity && !activity.value.trim()) {
      activity.setCustomValidity("Describe the programming activity before completing this task.");
      activity.reportValidity();
      return;
    }
    if (activity) activity.setCustomValidity("");
    const formData = new FormData(form);
    formData.set("state", submitter.value);
    activeMutations += 1;
    const card = form.closest("[data-item-id]");
    card.classList.add("is-saving");
    form.querySelectorAll("button").forEach(button => { button.disabled = true; });
    setSyncStatus("Saving…", "sync-saving");
    applyItem({
      id: card.dataset.itemId,
      state: submitter.value,
      state_label: stateLabels[submitter.value],
      staff_name: checklist.dataset.currentStaff,
      changed_at: "",
      changed_at_label: "Saving…",
    });
    try {
      const response = await fetch(form.action, {
        method: "POST",
        body: formData,
        headers: { "X-Requested-With": "XMLHttpRequest", Accept: "application/json" },
      });
      if (!response.ok) throw new Error(`Save failed with ${response.status}`);
      applyState(await response.json());
      setSyncStatus("Saved and shared", "sync-saved");
    } catch (error) {
      setSyncStatus("Save failed — checking server state", "sync-error");
      revision = "";
    } finally {
      activeMutations -= 1;
      await fetchState();
    }
  });

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) fetchState();
  });
  window.setInterval(fetchState, pollIntervalMs);
})();
