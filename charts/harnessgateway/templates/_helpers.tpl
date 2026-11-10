{{- define "hg.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}{{ .Release.Name | trunc 40 | trimSuffix "-" }}
{{- else -}}{{ printf "%s-%s" .Release.Name .Chart.Name | trunc 40 | trimSuffix "-" }}{{- end -}}
{{- end -}}

{{- define "hg.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/* selector labels of one component: (dict "ctx" $ "c" "api") */}}
{{- define "hg.select" -}}
app.kubernetes.io/name: {{ .ctx.Chart.Name }}
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
app.kubernetes.io/component: {{ .c }}
{{- end -}}

{{- define "hg.image" -}}{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}{{- end -}}
{{- define "hg.sandboxImage" -}}{{ .Values.sandbox.repository }}:{{ .Values.sandbox.tag | default .Chart.AppVersion }}{{- end -}}
{{- define "hg.secret" -}}{{ .Values.secrets.existingSecret | default (include "hg.fullname" .) }}{{- end -}}
{{- define "hg.boxesNamespace" -}}{{ .Values.boxes.namespace | default (printf "%s-boxes" .Release.Namespace) }}{{- end -}}
{{- define "hg.apiUrl" -}}http://{{ include "hg.fullname" . }}-api.{{ .Release.Namespace }}.svc.cluster.local:{{ .Values.api.service.port }}{{- end -}}
{{- define "hg.objectsInVolume" -}}{{ if not .Values.objects.url }}true{{ end }}{{- end -}}

{{/* What the door needs (it holds no database or operator credential). */}}
{{- define "hg.doorEnv" -}}
- name: HG_SECRET_KEY
  valueFrom: {secretKeyRef: {name: {{ include "hg.secret" . }}, key: secret-key}}
- name: HG_DOOR_KEY
  valueFrom: {secretKeyRef: {name: {{ include "hg.secret" . }}, key: door-key}}
- name: HG_API_URL
  value: {{ include "hg.apiUrl" . }}
- name: HG_DOOR
  value: hg-door:{{ .Values.door.port }}
{{- if .Values.caBundle.configMap }}
- name: HG_CA_FILE
  value: /etc/harnessgateway/ca/{{ .Values.caBundle.key }}
{{- end }}
{{- range $k, $v := .Values.config }}
- name: {{ $k }}
  value: {{ $v | toString | quote }}
{{- end }}
{{- with .Values.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/* The api's and worker's environment. */}}
{{- define "hg.env" -}}
- name: HG_OPERATOR_KEY
  valueFrom: {secretKeyRef: {name: {{ include "hg.secret" . }}, key: operator-key, optional: true}}
{{- if .Values.postgresql.enabled }}
- name: HG_DB_PASSWORD
  valueFrom: {secretKeyRef: {name: {{ include "hg.secret" . }}, key: db-password}}
- name: HG_DATABASE_URL
  value: postgresql://harnessgateway:$(HG_DB_PASSWORD)@{{ include "hg.fullname" . }}-postgres:5432/harnessgateway
{{- else }}
- name: HG_DATABASE_URL
  valueFrom: {secretKeyRef: {name: {{ required "externalDatabase.existingSecret is required with postgresql.enabled=false" .Values.externalDatabase.existingSecret }}, key: {{ .Values.externalDatabase.key }}}}
{{- end }}
- name: HG_REDIS_URL
  value: {{ if .Values.redis.enabled }}redis://{{ include "hg.fullname" . }}-redis:6379/0{{ else }}{{ required "externalRedis.url is required with redis.enabled=false" .Values.externalRedis.url }}{{ end }}
- name: HG_BLOB_URL
  value: {{ .Values.objects.url | default "file:///var/lib/harnessgateway-objects" | quote }}
- name: HG_CONSOLE_URL
  value: {{ .Values.console.url | quote }}
- name: HG_SANDBOX_IMAGE
  value: {{ include "hg.sandboxImage" . }}
- name: HG_PLATFORM_IMAGE
  value: {{ include "hg.image" . }}
- name: HG_SANDBOX_RUNTIME
  value: {{ .Values.sandbox.runtime }}
{{ include "hg.doorEnv" . }}
{{- end -}}

{{- define "hg.envFrom" -}}
{{- if .Values.objects.envSecret }}
envFrom:
  - secretRef: {name: {{ .Values.objects.envSecret }}}
{{- end }}
{{- end -}}

{{- define "hg.containerSecurity" -}}
securityContext:
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities: {drop: [ALL]}
{{- end -}}

{{/* /tmp, and the CA when one is given: (dict "ctx" $) */}}
{{- define "hg.commonMounts" -}}
- {name: tmp, mountPath: /tmp}
{{- if .Values.caBundle.configMap }}
- {name: ca, mountPath: /etc/harnessgateway/ca, readOnly: true}
{{- end }}
{{- end -}}

{{- define "hg.commonVolumes" -}}
- name: tmp
  emptyDir: {}
{{- if .Values.caBundle.configMap }}
- name: ca
  configMap: {name: {{ .Values.caBundle.configMap }}}
{{- end }}
{{- end -}}

{{- define "hg.podCommon" -}}
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
{{- range . }}
  - name: {{ . }}
{{- end }}
{{- end }}
securityContext:
{{ toYaml .Values.podSecurityContext | indent 2 }}
{{- end -}}

{{/* wait for a TCP port before the role starts: (dict "ctx" $ "name" "postgres" "host" "x" "port" 5432) */}}
{{- define "hg.waitFor" -}}
- name: wait-{{ .name }}
  image: {{ include "hg.image" .ctx }}
  imagePullPolicy: {{ .ctx.Values.image.pullPolicy }}
  command: [python, -c, "import socket,sys,time\nfor _ in range(600):\n  try: socket.create_connection((sys.argv[1], int(sys.argv[2])), 2).close(); break\n  except OSError: time.sleep(1)\nelse: sys.exit(1)", {{ .host | quote }}, {{ .port | quote }}]
  {{- include "hg.containerSecurity" .ctx | nindent 2 }}
  resources: {limits: {cpu: 100m, memory: 64Mi}}
{{- end -}}
