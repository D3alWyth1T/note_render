/* Note Display - Main JavaScript */

// Editor state
let isEditing = false;
let originalContent = '';

document.addEventListener('DOMContentLoaded', function() {
    initCheckboxHandlers();
    initEditor();
    initCollapsibleHeaders();
    initLiveReload();
    initLocalGraph();
});

/* Checkbox Functionality */

function initCheckboxHandlers() {
    const checkboxes = document.querySelectorAll('.task-list-item-checkbox');
    const notePath = window.location.pathname;

    checkboxes.forEach(function(checkbox) {
        checkbox.addEventListener('change', function() {
            const index = parseInt(this.dataset.checkboxIndex, 10);
            const checked = this.checked;
            toggleCheckbox(notePath, index, checked, this);
        });
    });
}

function toggleCheckbox(notePath, index, checked, element) {
    fetch('/api/toggle-checkbox', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({
            note_path: notePath,
            checkbox_index: index,
            checked: checked
        })
    })
    .then(function(response) { return response.json(); })
    .then(function(data) {
        if (!data.success) {
            // Revert checkbox state on error
            element.checked = !checked;
            console.error('Failed to toggle checkbox:', data.error);
            alert('Failed to save checkbox state: ' + (data.error || 'Unknown error'));
        }
    })
    .catch(function(error) {
        // Revert checkbox state on network error
        element.checked = !checked;
        console.error('Network error:', error);
        alert('Network error while saving checkbox state');
    });
}

/* Editor Functionality */

function initEditor() {
    const editButton = document.getElementById('edit-toggle');
    const saveButton = document.getElementById('save-note');
    const cancelButton = document.getElementById('cancel-edit');

    if (!editButton) return; // Not on a note page

    editButton.addEventListener('click', toggleEditMode);
    if (saveButton) saveButton.addEventListener('click', saveNote);
    if (cancelButton) cancelButton.addEventListener('click', cancelEdit);

    // Keyboard shortcuts
    document.addEventListener('keydown', function(event) {
        if (isEditing) {
            // Ctrl+S or Cmd+S to save
            if ((event.ctrlKey || event.metaKey) && event.key === 's') {
                event.preventDefault();
                saveNote();
            }
            // Escape to cancel
            if (event.key === 'Escape') {
                event.preventDefault();
                cancelEdit();
            }
        }
    });
}

function toggleEditMode() {
    if (isEditing) {
        cancelEdit();
    } else {
        enterEditMode();
    }
}

function enterEditMode() {
    const notePath = window.location.pathname;
    const noteView = document.getElementById('note-view');
    const noteEditor = document.getElementById('note-editor');
    const editButton = document.getElementById('edit-toggle');
    const textarea = document.getElementById('editor-textarea');

    // Fetch raw content
    fetch('/api/get-note-raw?path=' + encodeURIComponent(notePath))
        .then(function(response) { return response.json(); })
        .then(function(data) {
            if (data.error) {
                alert('Failed to load note: ' + data.error);
                return;
            }

            originalContent = data.content;
            textarea.value = data.content;

            noteView.classList.add('hidden');
            noteEditor.classList.remove('hidden');
            editButton.textContent = 'Cancel';
            isEditing = true;

            textarea.focus();
        })
        .catch(function(error) {
            console.error('Error loading note:', error);
            alert('Failed to load note for editing');
        });
}

function saveNote() {
    const notePath = window.location.pathname;
    const textarea = document.getElementById('editor-textarea');
    const content = textarea.value;

    fetch('/api/save-note', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({
            note_path: notePath,
            content: content
        })
    })
    .then(function(response) { return response.json(); })
    .then(function(data) {
        if (data.success) {
            // Reload page to show updated content
            window.location.reload();
        } else {
            alert('Failed to save note: ' + (data.error || 'Unknown error'));
        }
    })
    .catch(function(error) {
        console.error('Error saving note:', error);
        alert('Network error while saving note');
    });
}

function cancelEdit() {
    const noteView = document.getElementById('note-view');
    const noteEditor = document.getElementById('note-editor');
    const editButton = document.getElementById('edit-toggle');
    const textarea = document.getElementById('editor-textarea');

    // Check for unsaved changes
    if (textarea.value !== originalContent) {
        if (!confirm('You have unsaved changes. Discard them?')) {
            return;
        }
    }

    noteView.classList.remove('hidden');
    noteEditor.classList.add('hidden');
    editButton.textContent = 'Edit';
    isEditing = false;
}

/* Collapsible Headers Functionality */

function initCollapsibleHeaders() {
    const article = document.getElementById('note-view');
    if (!article) return;

    // Find all h2-h6 headers (skip h1 as it's the title)
    const headers = article.querySelectorAll('h2, h3, h4, h5, h6');

    headers.forEach(function(header) {
        // Add toggle button
        const toggle = document.createElement('span');
        toggle.className = 'header-toggle';
        toggle.textContent = '\u25BC'; // ▼
        toggle.addEventListener('click', function(e) {
            e.stopPropagation();
            toggleSection(header, toggle);
        });
        header.insertBefore(toggle, header.firstChild);

        // Make header itself clickable
        header.addEventListener('click', function() {
            toggleSection(header, toggle);
        });
    });

    // Restore collapsed state from localStorage
    restoreCollapsedState();
}

function toggleSection(header, toggle) {
    const isCollapsed = header.classList.toggle('collapsed');
    toggle.textContent = isCollapsed ? '\u25B6' : '\u25BC'; // ▶ or ▼

    // Get all siblings until next header of same or higher level
    const headerLevel = parseInt(header.tagName[1], 10);
    let sibling = header.nextElementSibling;

    while (sibling) {
        if (sibling.matches('h1, h2, h3, h4, h5, h6')) {
            const siblingLevel = parseInt(sibling.tagName[1], 10);
            if (siblingLevel <= headerLevel) break;
        }
        sibling.classList.toggle('section-hidden', isCollapsed);
        sibling = sibling.nextElementSibling;
    }

    // Save state to localStorage
    saveCollapsedState();
}

function getStorageKey() {
    return 'collapsed-headers:' + window.location.pathname;
}

function saveCollapsedState() {
    const article = document.getElementById('note-view');
    if (!article) return;

    const collapsedIds = [];
    const headers = article.querySelectorAll('h2.collapsed, h3.collapsed, h4.collapsed, h5.collapsed, h6.collapsed');
    headers.forEach(function(header) {
        if (header.id) {
            collapsedIds.push(header.id);
        }
    });

    if (collapsedIds.length > 0) {
        localStorage.setItem(getStorageKey(), JSON.stringify(collapsedIds));
    } else {
        localStorage.removeItem(getStorageKey());
    }
}

function restoreCollapsedState() {
    const stored = localStorage.getItem(getStorageKey());
    if (!stored) return;

    try {
        const collapsedIds = JSON.parse(stored);
        collapsedIds.forEach(function(id) {
            const header = document.getElementById(id);
            if (header) {
                const toggle = header.querySelector('.header-toggle');
                if (toggle) {
                    toggleSection(header, toggle);
                }
            }
        });
    } catch (e) {
        console.error('Error restoring collapsed state:', e);
        localStorage.removeItem(getStorageKey());
    }
}

/* Live Reload Functionality */

function initLiveReload() {
    // Don't enable live reload on graph page or search page
    if (window.location.pathname === '/graph' || window.location.pathname === '/search') {
        return;
    }

    let eventSource = null;
    let reconnectTimeout = null;

    function connect() {
        if (eventSource) {
            eventSource.close();
        }

        eventSource = new EventSource('/api/events');

        eventSource.addEventListener('connected', function() {
            console.log('Live reload connected');
        });

        eventSource.addEventListener('modified', function(event) {
            const changedPath = event.data;
            const currentPath = window.location.pathname;

            // Reload if the current note was modified (and we're not editing)
            if (changedPath === currentPath && !isEditing) {
                console.log('Note modified, reloading...');
                window.location.reload();
            }
        });

        eventSource.addEventListener('deleted', function(event) {
            const deletedPath = event.data;
            const currentPath = window.location.pathname;

            // Redirect to home if current note was deleted
            if (deletedPath === currentPath) {
                console.log('Note deleted, redirecting to home...');
                window.location.href = '/';
            }
        });

        eventSource.onerror = function() {
            console.log('Live reload disconnected, reconnecting...');
            eventSource.close();

            // Reconnect after a delay
            if (reconnectTimeout) {
                clearTimeout(reconnectTimeout);
            }
            reconnectTimeout = setTimeout(connect, 3000);
        };
    }

    connect();

    // Clean up on page unload
    window.addEventListener('beforeunload', function() {
        if (eventSource) {
            eventSource.close();
        }
        if (reconnectTimeout) {
            clearTimeout(reconnectTimeout);
        }
    });
}

/* Local Graph Panel */

let localGraphSimulation = null;

function initLocalGraph() {
    const toggleButton = document.getElementById('local-graph-toggle');
    const closeButton = document.getElementById('local-graph-close');
    const panel = document.getElementById('local-graph-panel');

    if (!toggleButton || !panel) return;

    toggleButton.addEventListener('click', function() {
        if (panel.classList.contains('hidden')) {
            openLocalGraph();
        } else {
            closeLocalGraph();
        }
    });

    if (closeButton) {
        closeButton.addEventListener('click', closeLocalGraph);
    }
}

function openLocalGraph() {
    const panel = document.getElementById('local-graph-panel');
    const container = document.getElementById('local-graph-container');
    const notePath = window.location.pathname;

    panel.classList.remove('hidden');

    // Clear previous graph
    container.innerHTML = '';

    // Fetch local graph data
    fetch('/api/local-graph?path=' + encodeURIComponent(notePath))
        .then(function(response) { return response.json(); })
        .then(function(data) {
            if (data.error) {
                container.innerHTML = '<p style="padding: 1rem; color: var(--text-secondary);">' + data.error + '</p>';
                return;
            }
            if (data.nodes.length === 0) {
                container.innerHTML = '<p style="padding: 1rem; color: var(--text-secondary);">No linked notes found.</p>';
                return;
            }
            renderLocalGraph(data, container);
        })
        .catch(function(error) {
            console.error('Error loading local graph:', error);
            container.innerHTML = '<p style="padding: 1rem; color: var(--text-secondary);">Error loading graph.</p>';
        });
}

function closeLocalGraph() {
    const panel = document.getElementById('local-graph-panel');
    panel.classList.add('hidden');

    // Stop simulation to save resources
    if (localGraphSimulation) {
        localGraphSimulation.stop();
        localGraphSimulation = null;
    }
}

function renderLocalGraph(data, container) {
    const width = container.clientWidth;
    const height = container.clientHeight;

    const svg = d3.select(container)
        .append('svg')
        .attr('width', width)
        .attr('height', height);

    const g = svg.append('g');

    // Add zoom behavior
    const zoom = d3.zoom()
        .scaleExtent([0.5, 3])
        .on('zoom', function(event) {
            g.attr('transform', event.transform);
        });
    svg.call(zoom);

    const nodes = data.nodes;
    const links = data.edges;

    // Calculate node sizes
    const minRadius = 8;
    const maxRadius = 20;
    const maxConnections = Math.max(...nodes.map(function(n) { return n.connections; }), 1);
    nodes.forEach(function(node) {
        node.radius = minRadius + (node.connections / maxConnections) * (maxRadius - minRadius);
        // Make current node slightly larger
        if (node.current) {
            node.radius = Math.max(node.radius, 15);
        }
    });

    // Create force simulation
    localGraphSimulation = d3.forceSimulation(nodes)
        .force('link', d3.forceLink(links).id(function(d) { return d.id; }).distance(70))
        .force('charge', d3.forceManyBody().strength(-150))
        .force('center', d3.forceCenter(width / 2, height / 2))
        .force('collision', d3.forceCollide().radius(function(d) { return d.radius + 10; }));

    // Draw links
    const link = g.append('g')
        .selectAll('line')
        .data(links)
        .join('line')
        .attr('class', 'link');

    // Draw nodes
    const node = g.append('g')
        .selectAll('g')
        .data(nodes)
        .join('g')
        .attr('class', function(d) { return 'node' + (d.current ? ' current' : ''); })
        .call(d3.drag()
            .on('start', dragstarted)
            .on('drag', dragged)
            .on('end', dragended));

    node.append('circle')
        .attr('r', function(d) { return d.radius; });

    node.append('text')
        .attr('dx', function(d) { return d.radius + 4; })
        .attr('dy', 4)
        .text(function(d) { return d.label; });

    // Click to navigate
    node.on('click', function(event, d) {
        if (!d.current) {
            window.location.href = d.path;
        }
    });

    // Update positions on tick
    localGraphSimulation.on('tick', function() {
        link
            .attr('x1', function(d) { return d.source.x; })
            .attr('y1', function(d) { return d.source.y; })
            .attr('x2', function(d) { return d.target.x; })
            .attr('y2', function(d) { return d.target.y; });

        node.attr('transform', function(d) { return 'translate(' + d.x + ',' + d.y + ')'; });
    });

    // Drag functions
    function dragstarted(event, d) {
        if (!event.active) localGraphSimulation.alphaTarget(0.3).restart();
        d.fx = d.x;
        d.fy = d.y;
    }

    function dragged(event, d) {
        d.fx = event.x;
        d.fy = event.y;
    }

    function dragended(event, d) {
        if (!event.active) localGraphSimulation.alphaTarget(0);
        d.fx = null;
        d.fy = null;
    }

    // Center on initial render
    setTimeout(function() {
        svg.call(zoom.transform, d3.zoomIdentity);
    }, 100);
}
