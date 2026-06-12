#!/usr/bin/env bash
# build.sh — runs during Render's build phase
# Writes the OCI config file from environment variables so the OCI SDK
# can authenticate without a local ~/.oci/config file.
set -e

pip install -r requirements.txt

# Write OCI config from env vars (set these as Secret Files or Env Vars in Render)
if [ -n "$OCI_KEY_CONTENT" ]; then
    mkdir -p /etc/oci
    cat > /etc/oci/config << OCICFG
[DEFAULT]
user=${OCI_USER_OCID}
fingerprint=${OCI_FINGERPRINT}
tenancy=${OCI_TENANCY_OCID}
region=${OCI_REGION}
key_file=/etc/oci/oci_api_key.pem
OCICFG

    # Write the private key
    echo "$OCI_KEY_CONTENT" > /etc/oci/oci_api_key.pem
    chmod 600 /etc/oci/oci_api_key.pem
    echo "OCI config written to /etc/oci/config"
else
    echo "WARNING: OCI_KEY_CONTENT not set — OCI SDK will not authenticate"
fi
