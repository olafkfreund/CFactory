{{/*
Expand the name of the chart.
*/}}
{{- define "cfactory.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
Truncated at 63 chars (DNS limit). Honours .Values.fullnameOverride.
*/}}
{{- define "cfactory.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Chart name + version label.
*/}}
{{- define "cfactory.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels — emitted on every resource.
*/}}
{{- define "cfactory.labels" -}}
helm.sh/chart: {{ include "cfactory.chart" . }}
{{ include "cfactory.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: cfactory
{{- end }}

{{/*
Selector labels — also used by the Service.
*/}}
{{- define "cfactory.selectorLabels" -}}
app.kubernetes.io/name: {{ include "cfactory.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
ServiceAccount name to use.
*/}}
{{- define "cfactory.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "cfactory.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Image reference: respects .Values.image.repository + tag, defaulting
to the chart's appVersion when tag is empty.
*/}}
{{- define "cfactory.image" -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion }}
{{- printf "%s:%s" .Values.image.repository $tag }}
{{- end }}

{{/*
Frontend (cockpit UI) image reference.
*/}}
{{- define "cfactory.frontend.image" -}}
{{- $tag := .Values.frontend.image.tag | default .Chart.AppVersion }}
{{- printf "%s:%s" .Values.frontend.image.repository $tag }}
{{- end }}
