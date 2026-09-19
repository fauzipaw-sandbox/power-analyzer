import streamlit as st
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from supabase import create_client, Client
import plotly.graph_objects as go

st.set_page_config(
    page_title="BBU Voltage Monitoring & Analysis",
    page_icon="⚡",
    layout="wide"
)

# ----------------- KONEKSI DATABASE -----------------
@st.cache_resource
def get_db_client() -> Client:
    url = st.secrets.get("SUPABASE_URL")
    key = st.secrets.get("SUPABASE_KEY")
    if not url or not key:
        st.error("Konfigurasi kredensial database belum lengkap di Settings > Secrets.")
        st.stop()
    return create_client(url, key)

db = get_db_client()

st.title("⚡ BBU Voltage Monitoring & Analysis")

# ================= SIDEBAR: OTOMATIS MULTI-UPLOAD =================
st.sidebar.header("📥 Ingest Data PM")
uploaded_files = st.sidebar.file_uploader(
    "Upload File Excel BBU Voltage (Bisa Banyak):", 
    type=["xlsx", "xls"],
    accept_multiple_files=True,
    help="Pilih satu atau beberapa file Excel. Data otomatis diproses dan disimpan ke database."
)

if "processed_file_names" not in st.session_state:
    st.session_state.processed_file_names = set()

def parse_voltage(val):
    if pd.isna(val) or val is None:
        return None
    try:
        val_str = str(val).strip().replace(",", ".")
        f_val = float(val_str)
        return None if np.isnan(f_val) else f_val
    except:
        return None

new_files_to_process = [
    f for f in (uploaded_files or []) 
    if f.name not in st.session_state.processed_file_names
]

if new_files_to_process:
    total_saved_all_files = 0
    with st.spinner(f"Memproses otomatis {len(new_files_to_process)} file ke database..."):
        for current_file in new_files_to_process:
            try:
                df_raw = pd.read_excel(current_file, engine="openpyxl")

                # 1. Deteksi dinamis nama kolom waktu, ME, dan voltage
                time_col = None
                me_col = None
                min_col = None
                avg_col = None
                max_col = None

                for col in df_raw.columns:
                    c = str(col).lower().replace(" ", "").replace("_", "").replace(".", "").replace("(", "").replace(")", "")
                    
                    if ("begintime" in c or "starttime" in c or "time" in c) and not time_col:
                        time_col = col
                    elif "managedelement" in c or "nename" in c or "site" in c:
                        me_col = col
                    elif "min" in c and ("volt" in c or "v" in c):
                        min_col = col
                    elif ("avg" in c or "mean" in c) and ("volt" in c or "v" in c):
                        avg_col = col
                    elif "max" in c and ("volt" in c or "v" in c):
                        max_col = col

                # Fallback jika hanya ada satu kolom 'voltage'
                if not min_col:
                    for col in df_raw.columns:
                        if "volt" in str(col).lower():
                            min_col = col
                            avg_col = col
                            max_col = col
                            break

                if not time_col or not me_col:
                    st.sidebar.error(f"Gagal mendeteksi kolom Waktu atau ME pada {current_file.name}")
                    continue

                # 2. Filter unit VPD jika kolom replaceable unit tersedia
                df_filtered = df_raw.copy()
                for col in df_filtered.columns:
                    c_low = str(col).lower()
                    if "replaceable" in c_low or "unit" in c_low:
                        has_vpd = df_filtered[col].astype(str).str.contains("VPD", case=False, na=False)
                        if has_vpd.any():
                            df_filtered = df_filtered[has_vpd]
                        break

                # 3. Bersihkan tanggal dan duplikat baris internal
                df_filtered["parsed_time"] = pd.to_datetime(df_filtered[time_col], errors="coerce")
                df_filtered["managed_element"] = df_filtered[me_col].astype(str).str.strip()
                df_clean = df_filtered.dropna(subset=["parsed_time", "managed_element"])
                df_clean = df_clean.drop_duplicates(subset=["parsed_time", "managed_element"], keep="last")

                # 4. Susun records dengan konversi float aman
                records = []
                for _, row in df_clean.iterrows():
                    val_min = parse_voltage(row[min_col]) if min_col and min_col in row else None
                    val_avg = parse_voltage(row[avg_col]) if avg_col and avg_col in row else None
                    val_max = parse_voltage(row[max_col]) if max_col and max_col in row else None

                    records.append({
                        "begin_time": row["parsed_time"].isoformat(),
                        "managed_element": row["managed_element"],
                        "min_voltage": val_min,
                        "avg_voltage": val_avg,
                        "max_voltage": val_max,
                    })

                # 5. Upsert batch ke database
                batch_size = 250
                for i in range(0, len(records), batch_size):
                    batch = records[i:i + batch_size]
                    db.table("bbu_voltage").upsert(
                        batch,
                        on_conflict="managed_element,begin_time"
                    ).execute()

                total_saved_all_files += len(records)
                st.session_state.processed_file_names.add(current_file.name)
            except Exception as e:
                st.sidebar.error(f"Error memproses {current_file.name}: {str(e)}")

        if total_saved_all_files > 0:
            st.sidebar.success(f"Berhasil menyimpan {total_saved_all_files} baris data!")
            st.cache_data.clear()
            st.rerun()

st.sidebar.markdown("---")

# ================= SIDEBAR: PARAMETER FILTER =================
st.sidebar.header("⚙️ Parameter Filter")
filter_mode = st.sidebar.radio("Mode Rentang Waktu:", ["Berdasarkan Data Terakhir", "Semua Data"])

if filter_mode == "Berdasarkan Data Terakhir":
    duration_hours = st.sidebar.slider(
        "Rentang Waktu Terakhir (Jam):",
        min_value=1,
        max_value=72,
        value=24,
        step=1,
        help="Geser untuk menentukan durasi mundur dari timestamp data paling akhir"
    )
else:
    duration_hours = None

threshold_voltage = st.sidebar.number_input(
    "Batas Voltage Drop (V):", 
    min_value=30.0, 
    max_value=60.0, 
    value=42.0, 
    step=0.5
)

# ================= AUTO LOAD SEMUA DATA DENGAN PAGINATION (BYPASS 1000 ROW LIMIT) =================
@st.cache_data(ttl=30)
def load_voltage_overview_paginated():
    all_rows = []
    chunk_size = 1000
    current_start = 0
    max_total_limit = 100000  # Kuota aman hingga 100.000 baris

    while current_start < max_total_limit:
        res = (
            db.table("bbu_voltage")
            .select("managed_element, min_voltage, avg_voltage, max_voltage, begin_time")
            .order("begin_time", desc=False)
            .range(current_start, current_start + chunk_size - 1)
            .execute()
        )
        data = res.data
        if not data:
            break
        all_rows.extend(data)
        if len(data) < chunk_size:
            break
        current_start += chunk_size

    return all_rows

try:
    with st.spinner("Mengambil seluruh data site dari database..."):
        all_raw_data = load_voltage_overview_paginated()
except Exception as e:
    st.error(f"Gagal membaca data dari server: {str(e)}")
    all_raw_data = []

# ================= FILTERING & PERHITUNGAN FREKUENSI =================
if not all_raw_data:
    st.info("Database masih kosong. Silakan unggah file Excel PM pada panel sebelah kiri.")
else:
    df_all = pd.DataFrame(all_raw_data)
    df_all["begin_time"] = pd.to_datetime(df_all["begin_time"])

    # Cutoff waktu mundur jika filter jam aktif
    cutoff_iso = None
    if duration_hours is not None and not df_all.empty:
        max_time = df_all["begin_time"].max()
        cutoff_time = max_time - timedelta(hours=int(duration_hours))
        cutoff_iso = cutoff_time.isoformat()
        df_filtered_view = df_all[df_all["begin_time"] >= cutoff_time]
    else:
        df_filtered_view = df_all

    # Filter khusus data numerik valid yang berada DI BAWAH batas voltage
    df_valid = df_filtered_view.dropna(subset=["min_voltage"]).copy()
    df_dropped = df_valid[df_valid["min_voltage"] < threshold_voltage]

    # Metrics Ringkasan
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Batas Voltage", f"< {threshold_voltage} V")
    col2.metric("Rentang Waktu", f"{duration_hours} Jam Terakhir" if duration_hours else "Semua Data")
    col3.metric("Total Site di Database", f"{df_all['managed_element'].nunique()} Site")
    col4.metric("Site Terdampak Drop", f"{df_dropped['managed_element'].nunique()} Site")

    st.markdown("---")

    # ================= TABEL DAFTAR SITE TERDAMPAK =================
    st.subheader(f"Daftar Site Terdampak Voltage < {threshold_voltage} V")

    selected_site = None

    if not df_dropped.empty:
        # Hitung frekuensi drop (berapa kali min_voltage < threshold_voltage)
        summary = (
            df_dropped.groupby("managed_element")
            .agg(
                frekuensi_drop=("min_voltage", "count"),
                min_v_recorded=("min_voltage", "min"),
                avg_v_recorded=("avg_voltage", "mean")
            )
            .reset_index()
            .sort_values(by=["frekuensi_drop", "min_v_recorded"], ascending=[False, True])
            .rename(columns={
                "managed_element": "Managed Element (Site)",
                "frekuensi_drop": "Frekuensi Drop",
                "min_v_recorded": "Voltage Terendah (V)",
                "avg_v_recorded": "Rata-rata Voltage (V)"
            })
        )
        
        summary["Voltage Terendah (V)"] = summary["Voltage Terendah (V)"].round(3)
        summary["Rata-rata Voltage (V)"] = summary["Rata-rata Voltage (V)"].round(3)

        st.caption("👉 **Klik salah satu baris site** pada tabel di bawah untuk melihat tren tegangannya.")

        # Tabel interaktif klik baris
        selection_event = st.dataframe(
            summary,
            use_container_width=True,
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row"
        )

        if selection_event and selection_event.selection and selection_event.selection.rows:
            selected_row_idx = selection_event.selection.rows[0]
            selected_site = summary.iloc[selected_row_idx]["Managed Element (Site)"]
        else:
            selected_site = summary.iloc[0]["Managed Element (Site)"]

        with st.expander("🔍 Lihat Rincian Log Kejadian Drop (Data Mentah)"):
            df_detail = df_dropped.copy().sort_values(by="begin_time", ascending=False)
            df_detail["Waktu (Lokal)"] = df_detail["begin_time"].dt.strftime("%Y-%m-%d %H:%M")
            st.dataframe(
                df_detail[["Waktu (Lokal)", "managed_element", "min_voltage", "avg_voltage", "max_voltage"]]
                .rename(columns={
                    "managed_element": "Site Name",
                    "min_voltage": "Min Voltage (V)",
                    "avg_voltage": "Avg Voltage (V)",
                    "max_voltage": "Max Voltage (V)"
                }),
                use_container_width=True,
                hide_index=True
            )
    else:
        st.success(f"Kondisi optimal. Tidak ditemukan site dengan voltage di bawah {threshold_voltage} V.")

    # ================= GRAFIK TREN DI BAWAH TABEL =================
    st.markdown("---")
    
    col_header, col_interval = st.columns([3, 1])
    with col_header:
        all_sites = sorted(df_all["managed_element"].unique().tolist())
        default_index = all_sites.index(selected_site) if (selected_site and selected_site in all_sites) else 0
        active_site = st.selectbox(
            "Site yang sedang dianalisis grafiknya:",
            options=all_sites,
            index=default_index,
            help="Bisa dipilih manual atau otomatis berubah saat baris tabel di atas diklik."
        )
    with col_interval:
        chart_interval = st.selectbox("Interval Grafik:", options=["15 Menit", "1 Jam", "Data Asli"])

    if active_site:
        with st.spinner(f"Memuat seluruh titik waktu untuk {active_site}..."):
            # Ambil semua data site tanpa limit
            query_site = (
                db.table("bbu_voltage")
                .select("begin_time, min_voltage, avg_voltage, max_voltage")
                .eq("managed_element", active_site)
                .order("begin_time", desc=False)
            )
            
            if cutoff_iso is not None:
                query_site = query_site.gte("begin_time", cutoff_iso)
            
            # Paginasi khusus site bila barisnya lebih dari 1000
            site_data = []
            s_start = 0
            while True:
                res_s = query_site.range(s_start, s_start + 999).execute()
                d_s = res_s.data
                if not d_s:
                    break
                site_data.extend(d_s)
                if len(d_s) < 1000:
                    break
                s_start += 1000

        if site_data and len(site_data) > 0:
            df_site = pd.DataFrame(site_data)
            df_site["begin_time"] = pd.to_datetime(df_site["begin_time"])
            df_site = df_site.set_index("begin_time").sort_index()

            # Resample dengan menjaga NaN agar jeda kosong terputus
            if chart_interval == "15 Menit":
                chart_df = df_site.resample("15min").agg({
                    "min_voltage": "min",
                    "avg_voltage": "mean",
                    "max_voltage": "max"
                })
            elif chart_interval == "1 Jam":
                chart_df = df_site.resample("1h").agg({
                    "min_voltage": "min",
                    "avg_voltage": "mean",
                    "max_voltage": "max"
                })
            else:
                chart_df = df_site[["min_voltage", "avg_voltage", "max_voltage"]].copy()

            # Rentang sumbu Y dinamis
            val_min = chart_df["min_voltage"].min()
            val_max = chart_df["max_voltage"].max()
            
            if pd.notna(val_min) and pd.notna(val_max):
                span = max(val_max - val_min, 2.0)
                y_bottom = max(0.0, float(val_min) - span * 0.15)
                y_top = float(val_max) + span * 0.15
                y_bottom = min(y_bottom, float(threshold_voltage) - 1.0)
            else:
                y_bottom, y_top = 35.0, 56.0

            fig = go.Figure()

            # Min Voltage (connectgaps=False agar jam tanpa data bolong)
            fig.add_trace(go.Scatter(
                x=chart_df.index,
                y=chart_df["min_voltage"],
                mode="lines+markers",
                name="Min Voltage (V)",
                connectgaps=False,
                line=dict(color="#EF4444", width=2),
                marker=dict(size=4)
            ))

            # Avg Voltage
            fig.add_trace(go.Scatter(
                x=chart_df.index,
                y=chart_df["avg_voltage"],
                mode="lines+markers",
                name="Avg Voltage (V)",
                connectgaps=False,
                line=dict(color="#3B82F6", width=2),
                marker=dict(size=4)
            ))

            # Max Voltage
            fig.add_trace(go.Scatter(
                x=chart_df.index,
                y=chart_df["max_voltage"],
                mode="lines+markers",
                name="Max Voltage (V)",
                connectgaps=False,
                line=dict(color="#10B981", width=2),
                marker=dict(size=4)
            ))

            # Garis Batas Ambang Threshold
            fig.add_hline(
                y=threshold_voltage,
                line_dash="dash",
                line_color="#DC2626",
                annotation_text=f"Threshold ({threshold_voltage} V)",
                annotation_position="bottom right"
            )

            fig.update_layout(
                title=f"Tren Pergerakan Voltage BBU - {active_site}",
                xaxis_title="Waktu",
                yaxis_title="Voltage (V)",
                yaxis=dict(range=[y_bottom, y_top]),
                hovermode="x unified",
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                margin=dict(l=20, r=20, t=50, b=20),
                template="plotly_dark"
            )

            st.plotly_chart(fig, use_container_width=True)

            # Statistik Cepat Site
            c1, c2, c3, c4 = st.columns(4)
            s_min = df_site["min_voltage"].min()
            s_avg = df_site["avg_voltage"].mean()
            s_max = df_site["max_voltage"].max()
            site_drop_count = (df_site["min_voltage"] < threshold_voltage).sum()

            c1.metric("Min Terendah", f"{s_min:.2f} V" if pd.notna(s_min) else "-")
            c2.metric("Rata-rata Tegangan", f"{s_avg:.2f} V" if pd.notna(s_avg) else "-")
            c3.metric("Max Tertinggi", f"{s_max:.2f} V" if pd.notna(s_max) else "-")
            c4.metric(f"Frekuensi Drop (< {threshold_voltage}V)", f"{site_drop_count} Kali")
        else:
            st.warning(f"Belum ada riwayat rekaman data untuk {active_site}.")
