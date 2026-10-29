
document.addEventListener("DOMContentLoaded", () => {
    const form = document.getElementById("issueForm");
    const message = document.getElementById("message");
    const issuesList = document.getElementById("issuesList");

    if (!form) {
        console.error("Complaint form #issueForm was not found.");
        return;
    }

    let isSubmitting = false;

    // Display a status message.
    function showMessage(text, isError = false) {
        if (!message) {
            if (isError) {
                alert(text);
            }
            return;
        }

        message.textContent = text;
        message.style.color = isError ? "#b42318" : "#147461";
        message.style.fontWeight = "600";
        message.style.whiteSpace = "pre-line";
    }

    // Escape user-provided content before displaying it in HTML.
    function escapeHTML(value) {
        return String(value ?? "").replace(/[&<>"']/g, char => ({
            "&": "&amp;",
            "<": "&lt;",
            ">": "&gt;",
            '"': "&quot;",
            "'": "&#39;"
        })[char]);
    }

    function photoURL(filename) {
        return "/uploads/" + encodeURIComponent(filename);
    }

    // Format the complaint submission date and time.
    function formatDateTime(value) {
        if (!value) {
            return "Not available";
        }

        const date = new Date(value);

        if (Number.isNaN(date.getTime())) {
            return String(value).replace("T", " ");
        }

        return date.toLocaleString("en-IN", {
            day: "2-digit",
            month: "short",
            year: "numeric",
            hour: "2-digit",
            minute: "2-digit",
            hour12: true
        });
    }

    function getComplaintID(issue) {
        if (issue.issue_id) {
            return issue.issue_id;
        }

        const id = issue.id;

        return id !== undefined && id !== null
            ? `CP-${String(id).padStart(4, "0")}`
            : "Unknown ID";
    }

    // Show a popup when the server detects a duplicate complaint.
    function showDuplicatePopup(result) {
        const duplicateID =
            result.duplicate_issue_id ||
            result.existing_issue_id ||
            result.issue_id ||
            (result.duplicate_of
                ? `CP-${String(result.duplicate_of).padStart(4, "0")}`
                : null);

        const reason = result.reason || result.message ||
            "A similar complaint has already been reported.";

        const location = result.existing_address
            ? `\nExisting complaint location: ${result.existing_address}`
            : "";

        const complaint = duplicateID
            ? `\nExisting complaint ID: ${duplicateID}`
            : "";

        const popupMessage =
            `${reason}${complaint}${location}\n\n` +
            "Your complaint was not submitted because a similar issue was detected.";

        // Use a modal if the report.html contains these elements.
        const modal = document.getElementById("duplicateModal");
        const modalMessage =
            document.getElementById("duplicateModalMessage");
        const closeButton =
            document.getElementById("closeDuplicateModal");

        if (modal && modalMessage) {
            modalMessage.textContent = popupMessage;
            modal.style.display = "flex";
            modal.setAttribute("aria-hidden", "false");

            if (closeButton && !closeButton.dataset.listenerAdded) {
                closeButton.addEventListener("click", () => {
                    modal.style.display = "none";
                    modal.setAttribute("aria-hidden", "true");
                });

                closeButton.dataset.listenerAdded = "true";
            }

            // Close the popup when the user clicks outside the dialog.
            if (!modal.dataset.outsideListenerAdded) {
                modal.addEventListener("click", event => {
                    if (event.target === modal) {
                        modal.style.display = "none";
                        modal.setAttribute("aria-hidden", "true");
                    }
                });

                modal.dataset.outsideListenerAdded = "true";
            }
        } else {
            // Fallback if the modal is not present in report.html.
            alert(popupMessage);
        }

        showMessage(
            duplicateID
                ? `Duplicate detected. Existing complaint: ${duplicateID}. Your complaint was not saved.`
                : "A similar complaint was detected. Your complaint was not saved.",
            true
        );
    }

    // Load all submitted complaints.
    async function loadIssues() {
        if (!issuesList) {
            return;
        }

        try {
            const response = await fetch("/api/issues", {
                headers: {
                    Accept: "application/json"
                }
            });

            const issues = await response.json();

            if (!response.ok) {
                throw new Error(
                    issues.error || "Could not load complaints."
                );
            }

            if (!Array.isArray(issues) || issues.length === 0) {
                issuesList.innerHTML =
                    "<p>No complaints have been submitted yet.</p>";
                return;
            }

            issuesList.innerHTML = issues.map(issue => {
                const id = escapeHTML(getComplaintID(issue));

                const category = escapeHTML(
                    issue.category || "Uncategorized"
                );

                const location = escapeHTML(
                    issue.address || issue.area || "Not provided"
                );

                const reportedTime = escapeHTML(
                    formatDateTime(issue.reported_at)
                );

                const priorityValue = Number(issue.priority);

                const priority = Number.isFinite(priorityValue)
                    ? `${priorityValue}/100`
                    : "Not calculated";

                const status = escapeHTML(
                    issue.status || "Pending"
                );

                const impactValue = issue.impact;

                const impact =
                    impactValue !== null &&
                    impactValue !== undefined &&
                    impactValue !== ""
                        ? `${escapeHTML(impactValue)}/5`
                        : "Not recorded";

                const duplicateID =
                    issue.duplicate_issue_id ||
                    (issue.duplicate_of
                        ? `CP-${String(issue.duplicate_of).padStart(4, "0")}`
                        : "");

                const duplicate = duplicateID
                    ? `<p style="color:#9a6700;">
                           Similar photo detected. Related complaint:
                           ${escapeHTML(duplicateID)}
                       </p>`
                    : "";

                const photo = issue.photo
                    ? `<img
                           src="${photoURL(issue.photo)}"
                           alt="Photo for ${id}"
                           loading="lazy"
                           style="width:100%;max-width:260px;border-radius:10px;margin-top:10px;"
                       >`
                    : "";

                return `
                    <article style="
                        background:white;
                        border:1px solid #dce8e5;
                        border-radius:14px;
                        padding:18px;
                        margin-bottom:14px;
                    ">
                        <h3>${id} — ${category}</h3>

                        <p>
                            <strong>Location:</strong>
                            ${location}
                        </p>

                        <p>
                            <strong>Reported On:</strong>
                            ${reportedTime}
                        </p>

                        <p>
                            <strong>Community Impact:</strong>
                            ${impact}
                        </p>

                        <p>
                            <strong>Priority:</strong>
                            ${priority}
                        </p>

                        <p>
                            <strong>Status:</strong>
                            ${status}
                        </p>

                        ${duplicate}
                        ${photo}
                    </article>
                `;
            }).join("");

        } catch (error) {
            console.error("Loading complaints failed:", error);

            issuesList.innerHTML = `
                <p style="color:#b42318;">
                    Could not load complaints. Check that Flask is running
                    and the /api/issues endpoint is working.
                </p>
            `;
        }
    }

    // Submit a complaint.
    form.addEventListener("submit", async event => {
        event.preventDefault();

        if (isSubmitting) {
            return;
        }

        const submitButton = form.querySelector(
            'button[type="submit"]'
        );

        if (!submitButton) {
            showMessage("Submit button was not found.", true);
            return;
        }

        const formData = new FormData(form);

        const category = String(
            formData.get("category") || ""
        ).trim();

        const address = String(
            formData.get("address") || ""
        ).trim();

        if (!category || !address) {
            showMessage(
                "Please select a category and enter the problem location.",
                true
            );
            return;
        }

        // Validate the optional image.
        const photoInput = document.getElementById("photo");
        const photo = photoInput?.files?.[0];

        if (photo) {
            const allowedTypes = [
                "image/png",
                "image/jpeg",
                "image/webp"
            ];

            if (!allowedTypes.includes(photo.type)) {
                showMessage(
                    "Please upload a PNG, JPG, JPEG, or WEBP image.",
                    true
                );
                return;
            }

            if (photo.size > 5 * 1024 * 1024) {
                showMessage(
                    "The photo must be 5 MB or smaller.",
                    true
                );
                return;
            }
        }

        isSubmitting = true;
        submitButton.disabled = true;
        submitButton.textContent = "Checking complaint...";
        showMessage("Checking for similar complaints...");

        try {
            const response = await fetch("/api/issues", {
                method: "POST",
                body: formData,
                headers: {
                    Accept: "application/json"
                }
            });

            // Handle JSON and unexpected server responses safely.
            let result;

            try {
                result = await response.json();
            } catch {
                throw new Error(
                    "The server returned an invalid response. Check the Flask terminal."
                );
            }

            // HTTP 409 means the backend rejected the duplicate.
            if (
                response.status === 409 ||
                result.duplicate === true ||
                result.is_duplicate === true
            ) {
                showDuplicatePopup(result);
                return;
            }

            if (!response.ok) {
                throw new Error(
                    result.error ||
                    result.message ||
                    "The complaint could not be saved."
                );
            }

            const complaintID = getComplaintID(result);

            const priorityValue = Number(result.priority);

            const priorityText = Number.isFinite(priorityValue)
                ? `${priorityValue}/100`
                : "pending calculation";

            showMessage(
                `Complaint submitted successfully! Your complaint ID is ${complaintID}. Priority: ${priorityText}.`
            );

            // Clear the form only after successful submission.
            form.reset();

            // Restore the category supplied in the URL, if any.
            const requestedCategory =
                new URLSearchParams(window.location.search)
                    .get("category");

            const categoryDropdown =
                document.getElementById("category");

            if (requestedCategory && categoryDropdown) {
                const matchingOption = Array.from(
                    categoryDropdown.options
                ).find(option =>
                    option.value.toLowerCase() ===
                    requestedCategory.toLowerCase()
                );

                if (matchingOption) {
                    categoryDropdown.value = matchingOption.value;
                }
            }

            // Reset the map marker, if supported by report.html.
            if (typeof window.resetCivicPulseMap === "function") {
                window.resetCivicPulseMap();
            }

            // Refresh the complaint list.
            await loadIssues();

        } catch (error) {
            console.error("Complaint submission failed:", error);

            showMessage(
                error.message ||
                "Something went wrong. Please try again.",
                true
            );
        } finally {
            isSubmitting = false;
            submitButton.disabled = false;
            submitButton.textContent = "Submit Complaint";
        }
    });

    // Initial complaint list.
    loadIssues();
});
