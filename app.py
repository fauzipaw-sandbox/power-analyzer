import streamlit as st
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
from supabase import create_client, Client

st.set_page_config(
    page_title="BBU Voltage Monitoring & Analysis",
    page_icon="⚡",
    layout="wide"
)

# ----------------- KONEKSI SUPABASE -----------------
@st.cache_resource
def get_supabase_client() -> Client:
    url = st.secrets.get("SUPABASE_URL")
    key = st.secrets.get("SUPABASE_KEY")
    if not url or not key:
        st.error("Kredensial SUPABASE_URL atau SUPABASE_KEY belum diset di Streamlit Secrets.")
        st.stop()
    return create_client(url, key)

supabase = get_supabase_client()

st.title("⚡ BBU Voltage Monitoring & Trend Analysis")

# ================= SIDEBAR: INGEST DATA =================
st.sidebar.header("📥 Ingest Data PM")
uploaded_file = st.sidebar.file_uploader("Upload File Excel BBU Voltage", type=["xlsx", "xls"])

if uploaded_file is not None:
    if st.sidebar.button("Proses & Simpan ke Supabase", use_container_width=True):
        with st.spinner("Membaca dan memproses file Excel..."):
            try:
                df_raw = pd.read_excel(uploaded_file, engine="openpyxl")
                
                # 1. Bersihkan format tanggal dan nama site
                df_clean = df_raw.copy()
                df_clean["parsed_time"] = pd.to_datetime(df_clean["Begin Time"], errors="coerce")
                df_clean["managed_element"] = df_clean["Managed Element"].astype(str).str.strip()
                df_clean = df_clean.dropna(subset=["parsed_time", "managed_element"])
                
                # 2. Hapus duplikat internal file (mencegah error Postgres 21000)
                df_clean = df_clean.drop_duplicates(subset=["parsed_time", "managed_element"], keep="last")
                
                # 3. Format payload data JSON
                records = []
                for _, row in df_clean.iterrows():
                    min_v = row.get("MinVoltageOfBBU(V)")
                    avg_v = row.get("AvgVoltageOfBBU(V)")
                    max_v = row.get("MaxVoltageOfBBU(V)")
                    
                    records.append({
                        "begin_time": row["parsed_time"].isoformat(),
                        "managed_element": row["managed_element"],
                        "min_voltage": float(min_v) if pd.notna(min_v) and not np.isnan(min_v) else None,
                        "avg_voltage": float(avg_v) if pd.notna(avg_v) and not np.isnan(avg_v) else None,
                        "max_voltage": float(max_v) if pd.notna(max_v) and not np.isnan(max_v) else None,
                    })

                # 4. Upload per batch
                batch_size = 250
                total_records = len(records)
                progress_bar = st.sidebar.progress(0)
                
                for i in range(0, total_records, batch_size):
                    batch = records[i:i + batch_size]
                    supabase.table("bbu_voltage").upsert(
                        batch,
                        on_conflict="managed_element,begin_time"
                    ).execute()
                    progress_bar.progress(min((i + batch_size) / total_records, 1.0))
                
                st.sidebar.success(f"Berhasil mengunggah {total_records} data unik!")
            except Exception as e:
                st.sidebar.error(f"Gagal memproses data: {str(e)}")

st.sidebar.markdown("---")

# ================= SIDEBAR: PARAMETER FILTER =================
st.sidebar.header("⚙️ Parameter Filter")
duration_hours = st.sidebar.number_input("Rentang Waktu Terakhir (Jam):", min_value=1, max_value=720, value=24, step=1)
threshold_voltage = st.sidebar.number_input("Batas Voltage Drop (V):", min_value=30.0, max_value=60.0, value=47.0, step=0.5)

# Cut-off waktu mundur dari sekarang (UTC)
time_limit = (datetime.now(timezone.utc) - timedelta(hours=int(duration_hours))).isoformat()

# ================= QUERY DATA SUPABASE =================
try:
    # Ambil data voltage yang berada dalam durasi waktu
    response = (
        supabase.table("bbu_voltage")
        .select("managed_element, min_voltage, avg_voltage, max_voltage, begin_time")
        .gte("begin_time", time_limit)
        .order("begin_time", desc=False)
        .execute()
    )
    all_data = response.data
except Exception as e:
    st.error(f"Gagal mengambil data dari Supabase: {str(e)}")
    all_data = []

# ================= TAMPILAN DASHBOARD =================
if not all_data:
    st.info(f"Belum ada data tercatat dalam {duration_hours} jam terakhir. Silakan upload file Excel PM di sidebar.")
else:
    df_all = pd.DataFrame(all_data)
    df_all["begin_time"] = pd.to_datetime(df_all["begin_time"])
    
    # Filter site yang mengalami drop di bawah threshold
    df_dropped = df_all[df_all["min_voltage"] < threshold_voltage]

    # Metrics Summary
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Batas Voltage", f"< {threshold_voltage} V")
    col2.metric("Rentang Waktu", f"{duration_hours} Jam Terakhir")
    col3.metric("Site Terdampak Drop", f"{df_dropped['managed_element'].nunique()} Site")
    col4.metric("Total Sampel Kejadian", f"{len(df_dropped)} Kali")

    st.markdown("---")

    # Tab Layout: Analisis Ranking Drop vs Grafik Tren
    tab1, tab2 = st.tabs(["📊 Ranking Drop Voltage", "📈 Grafik Tren Voltage"])

    with tab1:
        if not df_dropped.empty:
            summary = (
                df_dropped.groupby("managed_element")
                .agg(
                    total_drop=("min_voltage", "count"),
                    min_v_recorded=("min_voltage", "min"),
                    avg_v_recorded=("avg_voltage", "mean")
                )
                .reset_index()
                .sort_values(by=["total_drop", "min_v_recorded"], ascending=[False, True])
                .rename(columns={
                    "managed_element": "Managed Element (Site)",
                    "total_drop": "Frekuensi Drop (< Batas)",
                    "min_v_recorded": "Voltage Terendah (V)",
                    "avg_v_recorded": "Rata-rata Voltage (V)"
                })
            )
            
            # Format desimal
            summary["Voltage Terendah (V)"] = summary["Voltage Terendah (V)"].round(3)
            summary["Rata-rata Voltage (V)"] = summary["Rata-rata Voltage (V)"].round(3)

            st.subheader(f"Daftar Site Terdampak Voltage < {threshold_voltage} V")
            st.dataframe(summary, use_container_width=True, hide_index=True)

            with st.expander("🔍 Lihat Rincian Log Per Jam (Data Mentah)"):
                df_detail = df_dropped.copy().sort_values(by="begin_time", ascending=False)
                df_detail["Waktu (WIB/Local)"] = df_detail["begin_time"].dt.strftime("%Y-%m-%d %H:%M")
                st.dataframe(
                    df_detail[["Waktu (WIB/Local)", "managed_element", "min_voltage", "avg_voltage", "max_voltage"]]
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
            st.success(f"Aman! Tidak ada site dengan voltage di bawah {threshold_voltage} V dalam rentang {duration_hours} jam terakhir.")

    with tab2:
        st.subheader("Grafik Pergerakan Min, Avg, dan Max Voltage")
        
        # Pilihan Site: Prioritaskan site yang drop di urutan teratas
        dropped_sites = sorted(df_dropped["managed_element"].unique().tolist())
        all_sites = sorted(df_all["managed_element"].unique().tolist())
        site_list = dropped_sites + [s for s in all_sites if s not in dropped_sites]

        selected_site = st.selectbox("Pilih Managed Element (Site):", options=site_list)

        if selected_site:
            df_site = df_all[df_all["managed_element"] == selected_site].sort_values(by="begin_time")

            if not df_site.empty:
                # Siapkan data time-series untuk chart
                chart_data = df_site.set_index("begin_time")[["min_voltage", "avg_voltage", "max_voltage"]]
                chart_data.columns = ["Min Voltage", "Avg Voltage", "Max Voltage"]
                
                st.line_chart(chart_data, color=["#E53E3E", "#3182CE", "#38A169"])
                
                # Stat ringkas site terpilih
                s_min = df_site["min_voltage"].min()
                s_avg = df_site["avg_voltage"].mean()
                s_max = df_site["max_voltage"].max()
                
                c1, c2, c3 = st.columns(3)
                c1.metric("Min Tercatat", f"{s_min:.2f} V" if pd.notna(s_min) else "-")
                c2.metric("Rata-rata", f"{s_avg:.2f} V" if pd.notna(s_avg) else "-")
                c3.metric("Max Tercatat", f"{s_max:.2f} V" if pd.notna(s_max) else "-")
            else:
                st.warning("Tidak ada log riwayat untuk site ini.")
