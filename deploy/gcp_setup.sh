#!/usr/bin/env bash
# One-time Google Cloud setup + VM creation for Recal. Run from Cloud Shell or any
# machine with gcloud logged in as a project Owner:
#
#   PROJECT_ID=ai-builder-cup-9a0a1 bash deploy/gcp_setup.sh
#
# Idempotent: re-running skips what already exists. Prints the VM's public IP and
# the HTTPS URL judges will open once deploy/vm_install.sh has run on the VM.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID}"
REGION="${REGION:-us-central1}"
ZONE="${ZONE:-us-central1-a}"
VM_NAME="${VM_NAME:-recal-vm}"
MACHINE_TYPE="${MACHINE_TYPE:-e2-standard-2}"     # 2 vCPU / 8 GB: reel rendering needs the RAM
SA_NAME="${SA_NAME:-recal-runtime}"
BUCKET="${BUCKET:-${PROJECT_ID}-recal-media}"
FIRESTORE_LOCATION="${FIRESTORE_LOCATION:-nam5}"  # only used if no Firestore database exists yet
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud config set project "$PROJECT_ID" >/dev/null

exists() { "$@" >/dev/null 2>&1; }

echo "==> Enabling APIs (Vertex AI, Firestore, Storage, Compute, Calendar, Docs, Drive)"
gcloud services enable \
  aiplatform.googleapis.com firestore.googleapis.com storage.googleapis.com \
  compute.googleapis.com iamcredentials.googleapis.com logging.googleapis.com \
  secretmanager.googleapis.com \
  calendar-json.googleapis.com docs.googleapis.com drive.googleapis.com

echo "==> Runtime service account (the VM's identity: no keys on disk)"
exists gcloud iam service-accounts describe "$SA_EMAIL" ||
  gcloud iam service-accounts create "$SA_NAME" --display-name "Recal runtime"
for role in roles/aiplatform.user roles/datastore.user roles/logging.logWriter \
            roles/secretmanager.secretAccessor; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --condition=None --quiet \
    --member "serviceAccount:$SA_EMAIL" --role "$role" >/dev/null
done
# lets the VM sign Cloud Storage download URLs as itself (reel MP4/PDF downloads)
gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" --quiet \
  --member "serviceAccount:$SA_EMAIL" --role roles/iam.serviceAccountTokenCreator >/dev/null

echo "==> Firestore (default database)"
exists gcloud firestore databases describe --database='(default)' ||
  gcloud firestore databases create --database='(default)' --location="$FIRESTORE_LOCATION"

echo "==> Firestore vector indexes (768-dim gemini-embedding-001, cosine KNN)"
create_vector_index() {   # $1 collection group, $2 equality-filter field
  gcloud firestore indexes composite create --database='(default)' \
    --collection-group="$1" --query-scope=COLLECTION \
    --field-config="field-path=$2,order=ascending" \
    --field-config='field-path=embedding,vector-config={"dimension":"768","flat":"{}"}' \
    --async 2>&1 | grep -v "already exists" || true
}
create_vector_index nodes kind
create_vector_index style_memory polarity

echo "==> Media bucket gs://$BUCKET"
exists gcloud storage buckets describe "gs://$BUCKET" ||
  gcloud storage buckets create "gs://$BUCKET" --location="$REGION" --uniform-bucket-level-access
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --quiet \
  --member "serviceAccount:$SA_EMAIL" --role roles/storage.objectAdmin >/dev/null

echo "==> Static IP + firewall (HTTP/HTTPS only; SSH stays on Google's default rule)"
exists gcloud compute addresses describe recal-ip --region "$REGION" ||
  gcloud compute addresses create recal-ip --region "$REGION"
IP="$(gcloud compute addresses describe recal-ip --region "$REGION" --format='value(address)')"
exists gcloud compute firewall-rules describe recal-allow-web ||
  gcloud compute firewall-rules create recal-allow-web --allow tcp:80,tcp:443 \
    --target-tags recal-web --description "Recal: public web traffic"

echo "==> VM $VM_NAME ($MACHINE_TYPE, Ubuntu 24.04)"
exists gcloud compute instances describe "$VM_NAME" --zone "$ZONE" ||
  gcloud compute instances create "$VM_NAME" --zone "$ZONE" \
    --machine-type "$MACHINE_TYPE" --address "$IP" --tags recal-web \
    --image-family ubuntu-2404-lts-amd64 --image-project ubuntu-os-cloud \
    --boot-disk-size 30GB --service-account "$SA_EMAIL" --scopes cloud-platform

cat <<EOF

Done.
  VM:          $VM_NAME ($ZONE)  ->  $IP
  Public URL:  https://${IP//./-}.sslip.io   (after deploy/vm_install.sh; or set DOMAIN)
  Bucket:      gs://$BUCKET      (RECAL_MEDIA_BUCKET in .env)

Next:
  gcloud compute ssh $VM_NAME --zone $ZONE
  git clone https://github.com/Chaitanya674/Recall.git recal && cd recal
  bash deploy/vm_install.sh   # pulls .env from Secret Manager "recal-env" if it exists
EOF
