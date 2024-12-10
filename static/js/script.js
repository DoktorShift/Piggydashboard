// script.js

let totalDonations = 0; // Total donations
let transactionsData = []; // Store transaction history
const rowsPerPage = 10; // Number of rows to display per page
let currentPage = 1;
let lastUpdate = null; // Timestamp of the last update
let highlightThreshold = 2100; // Default threshold

// Function to show a toast notification
function showToast(message, isError = false) {
    const toastContainer = document.getElementById('toast-container');
    if (!toastContainer) {
        console.error('Toast container not found!');
        return;
    }

    const toast = document.createElement('div');
    toast.classList.add('toast');
    if (isError) {
        toast.classList.add('error');
    }
    toast.textContent = message;

    toastContainer.appendChild(toast);

    // Remove the toast after 3 seconds
    setTimeout(() => {
        toast.remove();
    }, 3000);
}

// Function to copy Lightning Address to clipboard
function copyText(element) {
    // Extract the address from the data-address attribute
    const address = element.getAttribute('data-address').trim();
    console.log('Attempting to copy Lightning Address:', address); // Debugging
    navigator.clipboard.writeText(address).then(() => {
        console.log('Lightning Address copied successfully');
        showToast('Lightning-Adresse in die Zwischenablage kopiert!');
    }).catch(err => {
        console.error('Error copying Lightning Address:', err);
        showToast('Fehler beim Kopieren der Lightning-Adresse.', true);
    });
}

// Function to copy LNURL to clipboard
function copyLnurl(element) {
    const lnurl = element.getAttribute('data-lnurl');
    console.log('Attempting to copy LNURL:', lnurl); // Debugging
    if (lnurl) {
        navigator.clipboard.writeText(lnurl).then(() => {
            console.log('LNURL copied successfully');
            showToast('LNURL in die Zwischenablage kopiert!');
        }).catch(err => {
            console.error('Error copying LNURL:', err);
            showToast('Fehler beim Kopieren der LNURL.', true);
        });
    } else {
        console.error('LNURL not found in the clicked element.');
        showToast('LNURL nicht gefunden!', true);
    }
}

// Function to format the date and time
function formatDate(dateString) {
    const currentDate = new Date();
    const givenDate = new Date(dateString);

    if (currentDate.toDateString() === givenDate.toDateString()) {
        // Today's donation: show only time
        return givenDate.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: true });
    } else {
        // Older donations: show date only
        return givenDate.toLocaleDateString([], { day: '2-digit', month: 'short', year: 'numeric' });
    }
}

// Function to update the UI with new data
function updateDonations(data) {
    console.log('Updating donations with data:', data); // Debugging
    totalDonations = data.total_donations;
    document.getElementById('totalDonations').textContent = `${totalDonations} Sats`;

    // Update latest donation
    if (data.donations.length > 0) {
        const latestDonation = data.donations[data.donations.length - 1];
        document.getElementById('donationHistory').textContent = `Letztes Sparen: ${latestDonation.amount} Sats - "${latestDonation.memo}"`;
    } else {
        document.getElementById('donationHistory').textContent = 'Letztes Sparen: Noch nichts.';
    }

    // Update transactions data
    transactionsData = data.donations;

    // Update Lightning Address and LNURL
    updateLightningAddress(data.lightning_address, data.lnurl);

    // Update Highlight Threshold
    if (data.highlight_threshold) {
        highlightThreshold = data.highlight_threshold;
        console.log(`Hervorhebungsschwellenwert aktualisiert auf: ${highlightThreshold} sats`);
    }

    // Render the table and pagination
    renderTable();
    renderPagination();
}

// Function to update the Lightning Address and LNURL in the DOM
function updateLightningAddress(lightningAddress, lnurl) {
    const copyField = document.getElementById('lightning-address-container');
    const addressSpan = document.getElementById('lightning-address');

    if (copyField && addressSpan) {
        if (lightningAddress && lightningAddress !== 'Unavailable') {
            copyField.setAttribute('data-address', lightningAddress);
            addressSpan.textContent = lightningAddress;
        } else {
            copyField.setAttribute('data-address', 'Unbekannte Lightning-Adresse');
            addressSpan.textContent = 'Unbekannte Lightning-Adresse';
        }
    } else {
        console.error('Lightning Address elements not found in the DOM.');
    }
}

// Function to render the transaction table
function renderTable() {
    const tableBody = document.getElementById('transactions');
    tableBody.innerHTML = '';

    const startIndex = (currentPage - 1) * rowsPerPage;
    const endIndex = startIndex + rowsPerPage;
    const visibleTransactions = transactionsData.slice().reverse().slice(startIndex, endIndex);

    if (visibleTransactions.length === 0) {
        tableBody.innerHTML = '<tr><td colspan="3" class="no-data">Noch keine Ersparnisse.</td></tr>';
    } else {
        visibleTransactions.forEach((transaction) => {
            const row = document.createElement('tr');

            // Check if donation is greater than highlight threshold
            if (transaction.amount > highlightThreshold) { // Use dynamic threshold
                row.classList.add('highlight');
            }

            row.innerHTML = `
                <td>${formatDate(transaction.date)}</td>
                <td>${transaction.memo}</td>
                <td>${transaction.amount} Sats</td>
            `;
            tableBody.appendChild(row);
        });
    }
}

// Function to render pagination
function renderPagination() {
    const pagination = document.getElementById('pagination');
    pagination.innerHTML = '';

    const totalPages = Math.ceil(transactionsData.length / rowsPerPage);

    for (let i = 1; i <= totalPages; i++) {
        const pageLink = document.createElement('a');
        pageLink.textContent = i;
        pageLink.href = '#';
        if (i === currentPage) {
            pageLink.classList.add('active');
        }
        pageLink.addEventListener('click', (e) => {
            e.preventDefault();
            currentPage = i;
            renderTable();
            renderPagination();
        });
        pagination.appendChild(pageLink);
    }
}

// Function to fetch initial donations data from the server
async function fetchInitialDonations() {
    try {
        const [donationsResponse, updatesResponse] = await Promise.all([
            fetch('/api/donations'),
            fetch('/donations_updates')
        ]);

        if (!donationsResponse.ok || !updatesResponse.ok) {
            throw new Error('Fehler beim Abrufen der initialen Daten');
        }

        const donationsData = await donationsResponse.json();
        const updatesData = await updatesResponse.json();

        // Update the UI with initial data
        updateDonations(donationsData);

        // Set the initial lastUpdate timestamp
        lastUpdate = new Date(updatesData.last_update);

    } catch (error) {
        console.error('Fehler beim Abrufen der initialen Ersparnisse:', error);
        showToast('Fehler beim Abrufen der initialen Ersparnisse.', true);
    }
}

// Function to check for updates using long-polling
async function checkForUpdates() {
    try {
        const response = await fetch('/donations_updates');
        if (!response.ok) {
            throw new Error('Fehler beim Abrufen der Updates');
        }

        const data = await response.json();
        const serverUpdate = new Date(data.last_update);

        if (!lastUpdate || serverUpdate > lastUpdate) {
            // New update detected
            lastUpdate = serverUpdate;
            // Fetch the latest donations data
            const donationsResponse = await fetch('/api/donations');
            if (!donationsResponse.ok) {
                throw new Error('Fehler beim Abrufen der aktualisierten Ersparnisse');
            }
            const donationsData = await donationsResponse.json();
            updateDonations(donationsData);
        }

    } catch (error) {
        console.error('Fehler beim Überprüfen der Updates:', error);
        showToast('Fehler beim Überprüfen der Updates.', true);
    } finally {
        // Schedule the next update check
        setTimeout(checkForUpdates, 5000); // Every 5 seconds
    }
}

// Initialize on page load
document.addEventListener("DOMContentLoaded", function() {
    // Fetch initial donations data
    fetchInitialDonations();
    // Start checking for updates
    checkForUpdates();
});
