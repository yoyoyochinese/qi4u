"use client";

import { AggregateMetrics } from "../types";

interface Props {
  metrics: AggregateMetrics;
  label: string;
}

export default function MetricsCard({ metrics, label }: Props) {
  return (
    <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
      <h4 className="font-semibold text-sm text-gray-600 dark:text-gray-300 mb-3">
        {label}
      </h4>
      <div className="grid grid-cols-2 gap-3 text-sm">
        <div>
          <p className="text-gray-500">Avg Hops</p>
          <p className="text-xl font-bold">{metrics.avg_hops}</p>
        </div>
        <div>
          <p className="text-gray-500">Avg Time (min)</p>
          <p className="text-xl font-bold">{metrics.avg_time}</p>
        </div>
        <div>
          <p className="text-gray-500">Min / Max Hops</p>
          <p className="font-semibold">
            {metrics.min_hops} / {metrics.max_hops}
          </p>
        </div>
        <div>
          <p className="text-gray-500">Min / Max Time</p>
          <p className="font-semibold">
            {metrics.min_time} / {metrics.max_time}
          </p>
        </div>
        <div className="col-span-2">
          <p className="text-gray-500">
            Accepted: {metrics.accepted_patients} / {metrics.total_patients}
          </p>
        </div>
      </div>
    </div>
  );
}
