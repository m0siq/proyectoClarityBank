import React, { useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  AlertTriangle,
  ArrowDownLeft,
  ArrowRight,
  ArrowUpRight,
  BarChart3,
  BrainCircuit,
  CalendarDays,
  CheckCircle2,
  ChevronDown,
  CircleDollarSign,
  CreditCard,
  Layers,
  Plus,
  RefreshCw,
  Search,
  Send,
  ShieldAlert,
  Sparkles,
  WalletCards,
} from "lucide-react";
import "./styles.css";

const API_BASE = window.__BANKING_CONFIG__?.apiBaseUrl || "";
const MONTH = "2026-05";

const categoryLabel = {
  groceries: "Supermercado",
  transport: "Transporte",
  leisure: "Ocio",
  housing: "Hogar",
  health: "Salud",
  utilities: "Suministros",
  income: "Ingresos",
  transfers: "Transferencias",
  other: "Otros",
};

function money(value) {
  const amount = Number(value || 0);
  return new Intl.NumberFormat("es-ES", {
    style: "currency",
    currency: "EUR",
  }).format(amount);
}

function compactDate(value) {
  if (!value) return "";
  return new Intl.DateTimeFormat("es-ES", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

async function api(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || response.statusText);
  }
  return response.json();
}

function Stat({ icon: Icon, label, value, tone }) {
  return (
    <div className={`stat ${tone || ""}`}>
      <Icon size={18} />
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function CategoryBadge({ category }) {
  return <span className={`badge cat-${category || "other"}`}>{categoryLabel[category] || category || "Otros"}</span>;
}

function TransactionRow({ tx, pending }) {
  const amount = Number(tx.amount || 0);
  const anomaly = tx.anomaly?.is_anomaly;
  return (
    <div className={`tx-row ${anomaly ? "is-anomaly" : ""} ${pending ? "is-pending" : ""}`}>
      <div className="tx-icon">
        {pending ? <RefreshCw size={18} className="spin" /> : amount >= 0 ? <ArrowDownLeft size={18} /> : <ArrowUpRight size={18} />}
      </div>
      <div className="tx-main">
        <div className="tx-top">
          <strong>{tx.merchant_raw || "Movimiento"}</strong>
          <span className={amount >= 0 ? "amount positive" : "amount"}>{money(amount)}</span>
        </div>
        <div className="tx-meta">
          <span>{compactDate(tx.timestamp)}</span>
          {tx.category && <CategoryBadge category={tx.category} />}
          {tx.final_classifier && <span className="micro">{tx.final_classifier.toUpperCase()}</span>}
          {pending && <span className="micro">Procesando</span>}
          {anomaly && (
            <span className="micro danger">
              <AlertTriangle size={12} /> Anomalía
            </span>
          )}
        </div>
        {anomaly && tx.anomaly?.reason && <p className="tx-reason">{tx.anomaly.reason}</p>}
      </div>
    </div>
  );
}

function App() {
  const [users, setUsers] = useState([]);
  const [selectedUser, setSelectedUser] = useState("");
  const [transactions, setTransactions] = useState([]);
  const [profile, setProfile] = useState(null);
  const [insight, setInsight] = useState(null);
  const [feedback, setFeedback] = useState(null);
  const [pendingTx, setPendingTx] = useState(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [query, setQuery] = useState("");
  const [form, setForm] = useState({
    merchant_raw: "MERCADONA SUPERMERC",
    amount: "-24.90",
    currency: "EUR",
  });
  const [mlopsResult, setMlopsResult] = useState(null);

  useEffect(() => {
    loadUsers();
  }, []);

  useEffect(() => {
    if (!selectedUser) return;
    loadAccount(selectedUser);
  }, [selectedUser]);

  async function loadUsers() {
    setLoading(true);
    const data = await api(`/api/users?month=${MONTH}`);
    setUsers(data);
    setSelectedUser((current) => current || data[0]?.user_id || "");
    setLoading(false);
  }

  async function loadAccount(userId) {
    const [txs, profileData, insightData, feedbackData] = await Promise.all([
      api(`/api/users/${userId}/transactions?month=${MONTH}&limit=180`),
      api(`/api/users/${userId}/profile`).catch(() => null),
      api(`/api/insights?user_id=${userId}&year_month=${MONTH}`),
      api(`/api/mlops/feedback?year_month=${MONTH}`),
    ]);
    setTransactions(txs);
    setProfile(profileData);
    setInsight(insightData.found ? insightData.insight : null);
    setFeedback(feedbackData);
  }

  const filteredTransactions = useMemo(() => {
    const term = query.trim().toLowerCase();
    if (!term) return transactions;
    return transactions.filter((tx) => `${tx.merchant_raw} ${tx.category}`.toLowerCase().includes(term));
  }, [transactions, query]);

  const monthTotals = useMemo(() => {
    return transactions.reduce(
      (acc, tx) => {
        const amount = Number(tx.amount || 0);
        if (amount < 0) acc.spent += Math.abs(amount);
        else acc.income += amount;
        if (tx.anomaly?.is_anomaly) acc.anomalies += 1;
        return acc;
      },
      { spent: 0, income: 0, anomalies: 0 },
    );
  }, [transactions]);

  const selectedProfile = users.find((user) => user.user_id === selectedUser);

  async function submitTransaction(event) {
    event.preventDefault();
    setBusy("transaction");
    const sent = await api("/api/transactions", {
      method: "POST",
      body: JSON.stringify({
        user_id: selectedUser,
        merchant_raw: form.merchant_raw,
        amount: form.amount,
        currency: form.currency,
      }),
    });
    const pending = {
      id: sent.transaction_id,
      transaction_id: sent.transaction_id,
      user_id: selectedUser,
      merchant_raw: form.merchant_raw,
      amount: form.amount,
      timestamp: new Date().toISOString(),
    };
    setPendingTx(pending);
    pollTransaction(sent.transaction_id, selectedUser);
    setBusy("");
  }

  async function pollTransaction(transactionId, userId) {
    for (let i = 0; i < 30; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 1500));
      const status = await api(`/api/transactions/${transactionId}?user_id=${userId}`);
      if (status.found) {
        setPendingTx(null);
        await loadAccount(userId);
        return;
      }
    }
  }

  async function runColdPath() {
    setBusy("cold");
    const result = await api("/api/cold-path/run", {
      method: "POST",
      body: JSON.stringify({ year_month: MONTH, user_id: selectedUser }),
    });
    setInsight(result.insights?.[0] || null);
    setBusy("");
  }

  async function runMlops() {
    setBusy("mlops");
    const result = await api("/api/mlops/run-local", {
      method: "POST",
      body: JSON.stringify({ year_month: MONTH, publish_to_blob: false }),
    });
    setMlopsResult(result);
    const feedbackData = await api(`/api/mlops/feedback?year_month=${MONTH}`);
    setFeedback(feedbackData);
    setBusy("");
  }

  if (loading) {
    return (
      <main className="shell loading-shell">
        <RefreshCw className="spin" />
      </main>
    );
  }

  return (
    <main className="shell">
      <section className="topbar">
        <div className="brand">
          <div className="brand-mark">i</div>
          <span>imagin</span>
        </div>
        <div className="account-switch">
          <WalletCards size={18} />
          <select value={selectedUser} onChange={(event) => setSelectedUser(event.target.value)}>
            {users.map((user) => (
              <option key={user.user_id} value={user.user_id}>
                {user.display_name}
              </option>
            ))}
          </select>
          <ChevronDown size={16} />
        </div>
      </section>

      <section className="hero">
        <div className="balance-card">
          <span>Cuenta activa</span>
          <strong>{selectedProfile?.display_name || selectedUser}</strong>
          <p>{selectedUser}</p>
          <div className="card-dots">
            <i />
            <i />
            <i />
            <i />
          </div>
        </div>
        <div className="summary-grid">
          <Stat icon={CircleDollarSign} label="Ingresos" value={money(monthTotals.income)} />
          <Stat icon={CreditCard} label="Gasto" value={money(monthTotals.spent)} />
          <Stat icon={ShieldAlert} label="Alertas" value={monthTotals.anomalies} tone={monthTotals.anomalies ? "hot" : ""} />
          <Stat icon={Layers} label="Histórico" value={profile?.transactions_count || selectedProfile?.transactions_count || 0} />
        </div>
      </section>

      <section className="layout">
        <div className="panel movements">
          <div className="panel-head">
            <div>
              <h1>Movimientos</h1>
              <span>Mayo 2026</span>
            </div>
            <label className="search">
              <Search size={16} />
              <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Buscar" />
            </label>
          </div>
          <div className="tx-list">
            {pendingTx && <TransactionRow tx={pendingTx} pending />}
            {filteredTransactions.map((tx) => (
              <TransactionRow key={tx.id} tx={tx} />
            ))}
          </div>
        </div>

        <aside className="side">
          <form className="panel action-panel" onSubmit={submitTransaction}>
            <div className="panel-head compact">
              <h2>Nuevo movimiento</h2>
              <Plus size={18} />
            </div>
            <label>
              Comercio
              <input
                value={form.merchant_raw}
                onChange={(event) => setForm({ ...form, merchant_raw: event.target.value })}
              />
            </label>
            <label>
              Importe
              <input
                value={form.amount}
                onChange={(event) => setForm({ ...form, amount: event.target.value })}
              />
            </label>
            <div className="quick-buttons">
              <button type="button" onClick={() => setForm({ ...form, merchant_raw: "XyzXyzXyz.xyz", amount: "-2333.45" })}>
                Cargo inusual
              </button>
              <button type="button" onClick={() => setForm({ ...form, merchant_raw: "MERCADONA SUPERMERC", amount: "-28.60" })}>
                Compra normal
              </button>
            </div>
            <button className="primary" disabled={busy === "transaction"}>
              {busy === "transaction" ? <RefreshCw className="spin" size={16} /> : <Send size={16} />}
              Enviar
            </button>
          </form>

          <section className="panel insight-panel">
            <div className="panel-head compact">
              <h2>Análisis mensual</h2>
              <CalendarDays size={18} />
            </div>
            <p>{insight?.summary_text || "Pulsa actualizar para calcular el análisis mensual de esta cuenta."}</p>
            <button className="secondary" onClick={runColdPath} disabled={busy === "cold"}>
              {busy === "cold" ? <RefreshCw className="spin" size={16} /> : <BarChart3 size={16} />}
              Actualizar análisis
            </button>
          </section>

          <section className="panel ml-panel">
            <div className="panel-head compact">
              <h2>Categorización</h2>
              <BrainCircuit size={18} />
            </div>
            <div className="ml-stats">
              <span>Casos pendientes</span>
              <strong>{feedback?.total || 0}</strong>
            </div>
            <div className="label-grid">
              {Object.entries(feedback?.labels || {}).map(([label, value]) => (
                <span key={label}>
                  {categoryLabel[label] || label}
                  <b>{value}</b>
                </span>
              ))}
            </div>
            {mlopsResult && (
              <div className="result-box">
                <CheckCircle2 size={16} />
                <span>
                  Precisión {Math.round((mlopsResult.precision_at_1 || 0) * 100)}% con {mlopsResult.feedback_records} casos
                </span>
              </div>
            )}
            <button className="secondary" onClick={runMlops} disabled={busy === "mlops"}>
              {busy === "mlops" ? <RefreshCw className="spin" size={16} /> : <Sparkles size={16} />}
              Recalibrar
            </button>
          </section>
        </aside>
      </section>
    </main>
  );
}

createRoot(document.getElementById("root")).render(<App />);
