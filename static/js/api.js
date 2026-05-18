const API = {
    async fetch(url, options = {}) {
        const token = localStorage.getItem('access_token');
        const headers = {
            'Content-Type': 'application/json',
            ...options.headers,
        };
        if (token) headers['Authorization'] = `Bearer ${token}`;

        const response = await fetch(url, { ...options, headers });
        const result = await response.json();

        if (response.status === 401) {
            window.location.href = '/login';
            return;
        }

        if (!result.success) {
            this.showError(result.message);
            throw new Error(result.message);
        }
        return result.data;
    },

    showError(msg) {
        // Implementation of a toast or alert
        alert("Error: " + msg);
    }
};