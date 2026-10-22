{{- define "pg-ai-monitor.name" -}}
{{- .Chart.Name -}}
{{- end -}}

{{- define "pg-ai-monitor.fullname" -}}
{{- .Release.Name -}}
{{- end -}}

{{- define "pg-ai-monitor.namespace" -}}
{{- default .Release.Namespace .Values.namespaceOverride -}}
{{- end -}}

{{- define "pg-ai-monitor.labels" -}}
app.kubernetes.io/name: {{ include "pg-ai-monitor.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}

{{- define "pg-ai-monitor.selectorLabels" -}}
app.kubernetes.io/name: {{ include "pg-ai-monitor.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "pg-ai-monitor.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "pg-ai-monitor.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}
