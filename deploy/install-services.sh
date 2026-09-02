#!/bin/bash
# GPU Resource Manager - One-click service installer
# Run with: sudo bash install-services.sh

set -e

echo "==> Installing GPU Manager Backend service..."
cp gpu-manager-backend.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable gpu-manager-backend
systemctl start gpu-manager-backend
echo "    Done. Status: $(systemctl is-active gpu-manager-backend)"

echo ""
echo "==> Installing GPU Manager Frontend service..."
cp gpu-manager-frontend.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable gpu-manager-frontend
systemctl start gpu-manager-frontend
echo "    Done. Status: $(systemctl is-active gpu-manager-frontend)"

echo ""
echo "==> All services installed. Checking status..."
systemctl status gpu-manager-backend --no-pager -l
echo ""
systemctl status gpu-manager-frontend --no-pager -l

echo ""
echo "==> Setting up Nginx..."
if command -v nginx &>/dev/null; then
    cp gpu-manager.nginx.conf /etc/nginx/sites-available/gpu-manager
    ln -sf /etc/nginx/sites-available/gpu-manager /etc/nginx/sites-enabled/
    rm -f /etc/nginx/sites-enabled/default
    nginx -t && systemctl reload nginx
    echo "    Nginx configured and reloaded."
else
    echo "    WARNING: Nginx not installed. Install with:"
    echo "      sudo apt install nginx -y"
    echo "    Then re-run this script or manually configure:"
    echo "      sudo cp deploy/gpu-manager.nginx.conf /etc/nginx/sites-available/gpu-manager"
    echo "      sudo ln -sf /etc/nginx/sites-available/gpu-manager /etc/nginx/sites-enabled/"
    echo "      sudo rm -f /etc/nginx/sites-enabled/default"
    echo "      sudo nginx -t && sudo systemctl reload nginx"
fi

echo ""
echo "==> All services installed. Checking status..."
systemctl status gpu-manager-backend --no-pager -l
echo ""
systemctl status gpu-manager-frontend --no-pager -l

echo ""
echo "==> To view live logs:"
echo "  sudo journalctl -u gpu-manager-backend -f"
echo "  sudo journalctl -u gpu-manager-frontend -f"
