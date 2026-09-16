'use client';
import { useState, useEffect } from 'react';
import { createClient } from '@supabase/supabase-js';

// Pastikan lo udah set Environment Variables di Vercel nanti
const supabase = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL!,
  process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!
);

export default function VoltageAnalysisDashboard() {
  const [duration, setDuration] = useState(3); // Default 3 jam terakhir
  const [threshold, setThreshold] = useState(47); // Default voltage < 47
  const [results, setResults] = useState<{ site: string; count: number }[]>([]);
  const [loading, setLoading] = useState(false);

  const fetchAnalysis = async () => {
    setLoading(true);
    // Kalkulasi waktu mundur berdasarkan input jam
    const timeLimit = new Date(Date.now() - duration * 60 * 60 * 1000).toISOString();

    // Query data dari Supabase
    const { data, error } = await supabase
      .from('bbu_voltage')
      .select('managed_element, min_voltage')
      .gte('begin_time', timeLimit)
      .lt('min_voltage', threshold);

    if (error) {
      console.error("Error fetching data:", error);
      setLoading(false);
      return;
    }

    // Hitung kemunculan (count) per site
    const counts: Record = {};
    data.forEach((row) => {
      counts[row.managed_element] = (counts[row.managed_element] || 0) + 1;
    });

    // Format ke array dan urutkan dari yang paling sering drop
    const formattedResults = Object.entries(counts)
      .map(([site, count]) => ({ site, count }))
      .sort((a, b) => b.count - a.count);

    setResults(formattedResults);
    setLoading(false);
  };

  // Otomatis fetch ulang kalau durasi atau threshold diganti
  useEffect(() => {
    fetchAnalysis();
  }, [duration, threshold]);

  return (
