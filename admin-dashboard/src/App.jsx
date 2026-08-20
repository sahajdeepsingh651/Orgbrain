import { useEffect, useState } from 'react';
import './App.css';

export default function App() {
  const [activities, setActivities] = useState([]);
  const [loading, setLoading] = useState(true);

  const fetchActivities = async () => {
    try {
      // The context bus backend runs on port 8000
      const res = await fetch(`http://${window.location.hostname}:8000/v1/agent-activity`, {
        headers: {
          'Authorization': 'Bearer token-220834002a083aa0' // Using user's actual token
        }
      });
      const data = await res.json();
      setActivities(data.results || []);
    } catch (e) {
      console.error('Failed to fetch admin data', e);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchActivities();
    const interval = setInterval(fetchActivities, 5000);
    return () => clearInterval(interval);
  }, []);

  return (
    <div className="admin-container">
      <aside className="admin-sidebar">
        <div className="sidebar-header">
          <div className="logo-box">
            <div className="logo-inner"></div>
          </div>
          <div>
            <h2>Context Bus</h2>
            <span>Admin Dashboard</span>
          </div>
        </div>
        <nav className="sidebar-nav">
          <div className="nav-item active">
            <span>▤</span>
            <span>All Passports</span>
          </div>
          <div className="nav-item">
            <span>⚙</span>
            <span>Settings</span>
          </div>
        </nav>
      </aside>

      <main className="admin-main">
        <header className="admin-header">
          <div>
            <h1>Global Context Bus</h1>
            <p>Live view of all context records uploaded across the organization.</p>
          </div>
          <div className="header-status">
            <span className="status-badge">● Live (0.0.0.0:8000)</span>
          </div>
        </header>

        <div className="content-area">
          {loading ? (
            <div className="loading">Loading records...</div>
          ) : activities.length === 0 ? (
            <div className="empty-state">No context passports found on the bus.</div>
          ) : (
            <div className="table-container">
              <table className="admin-table">
                <thead>
                  <tr>
                    <th>Record ID</th>
                    <th>Title</th>
                    <th>Author</th>
                    <th>Visibility</th>
                    <th>Status</th>
                    <th>Date</th>
                  </tr>
                </thead>
                <tbody>
                  {activities.map((act) => (
                    <tr key={act.record_id}>
                      <td className="mono">{act.record_id.substring(0, 8)}</td>
                      <td className="title-cell">{act.title}</td>
                      <td>{act.author_user_id || 'Unknown'}</td>
                      <td>
                        <span className={`badge visibility-${act.visibility || 'team'}`}>
                          {act.visibility || 'team'}
                        </span>
                      </td>
                      <td>{act.status || 'in_progress'}</td>
                      <td>{new Date(act.created_at * 1000).toLocaleString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </main>
    </div>
  );
}
