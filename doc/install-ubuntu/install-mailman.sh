#!/bin/bash


# Script to install Mailman 2 running on Python 3 on Ubuntu 26.04

# Usage example:
# ./install-mailman.sh lists.example.com webmaster@example.com

# Uses mailman2-python3 fork at:
# https://github.com/jaredmauch/mailman2-python3

# Installation documentation (for older Mailman2/Python2) at:
# https://www.gnu.org/software/mailman/mailman-install.pdf
# Useful notes (for older Mailman2/Python2 Ubuntu package) at:
# https://help.ubuntu.com/community/Mailman

# NB You must ensure DNS hostname is set to the machine to ensure succesful SSL certificate creation


# Stop on error
set -e

# Ensure this script is run as root
if [ "$(id -u)" != "0" ]; then
	echo "This script must be run as root." 1>&2
	exit 1
fi

# Require hostname and e-mail arguments
display_usage() {
	printf "Usage:\n./install-mailman.sh lists.example.com webmaster@example.com\n"
}
if [  $# -le 1 ]	# If less than two arguments supplied, display usage
then
	display_usage
	exit 1
fi
hostname=$1
email=$2

# Update/patch machine
apt-get update
apt-get -y upgrade
apt-get -y dist-upgrade
apt-get -y autoremove

# 1 Installation Requirements: Install Python3
apt-get install -y build-essential
gcc --version
apt-get install python3

# 2.1 Add the group and user
groupadd -f mailman
id -u mailman &>/dev/null || useradd -c"GNU Mailman" -s /usr/sbin/nologin --no-create-home -g mailman mailman

# Mailserver installation
apt-get -y install exim4
usermod -a -G Debian-exim mailman

# 2.2 Create the directory where the installation will be created
# NB If you use a directory such as /usr/local/mailman/ , you will need to logs and other directories to avoid AppArmor restrictions causing "OSError: [Errno 30] Read-only file system: '/usr/local/mailman/logs/error'"
#    See: https://github.com/jaredmauch/mailman2-python3/issues/23 and https://linux-audit.com/systemd/settings/units/protectsystem/
prefix=/var/lib/mailman
mkdir -p $prefix
chgrp -R mailman $prefix
chmod -R a+rx,g+ws $prefix

# Obtain distribution
# See: https://github.com/jaredmauch/mailman2-python3/
installDir=/tmp/mailman2-python3/
if [ ! -d $installDir ]; then
	apt-get install -y git
	git clone https://github.com/jaredmauch/mailman2-python3.git $installDir
fi
chown -R mailman $installDir

# 3 Build and install Mailman
# 3.1 Run configure
# Build, first adding build/runtime dependencies
apt-get install -y python3-dnspython python3-pip python3-legacy-cgi python3-html2text gettext python3-bsddb3
# NB The following can be added, but it will be more self-explanatory to add these in $prefix/Mailman/mm_cfg.py: " --with-mailhost=$hostname --with-urlhost=$hostname"
cd $installDir
sudo -H -u mailman bash -c "./configure --prefix=$prefix --with-mail-gid=Debian-exim --with-cgi-gid=www-data"

# 3.2 Make and install
sudo -H -u mailman bash -c 'make'
if [ ! -d "$prefix/Mailman/" ]; then
	sudo -H -u mailman bash -c 'make install'
fi
cd -

# 4 Check your installation
cd $prefix
sudo -H -u mailman bash -c "bin/check_perms -f"
cd -

# 7 Review your site defaults
# Add in config
cp ./mm_cfg.py $prefix/Mailman/mm_cfg.py
chown mailman:mailman $prefix/Mailman/mm_cfg.py
sed -i "s/lists.example.com/${hostname}/g" $prefix/Mailman/mm_cfg.py

# 5 Set up your web server
apt-get install -y apache2
a2enmod rewrite cgid

# Add HTTP VirtualHost
cp -pr ./lists.conf /etc/apache2/sites-available/
sed -i "s/lists.example.com/${hostname}/g" /etc/apache2/sites-available/lists.conf
sed -i "s/webmaster@example.com/${email}/g" /etc/apache2/sites-available/lists.conf
mkdir -p /var/www/lists/
a2ensite lists
service apache2 restart

# Create SSL certificate, and enable the HTTPS (SSL) VirtualHost using this newly-created certificate
set +e		# Allow this section to fail
apt-get install -y certbot
a2enmod ssl
certbot --agree-tos --no-eff-email certonly --keep-until-expiring --webroot -w /var/www/lists/ --email $email -d $hostname
if [ -f "/etc/letsencrypt/live/${hostname}/fullchain.pem" ]; then
	sed -i "s/##//g" /etc/apache2/sites-available/lists.conf	# Uncomment the ## lines from the template
fi
service apache2 restart
set -e		# Revert to stop on fail

# 6 Set up your mail server
# 6.2 Using the Exim mail server
# See: https://help.ubuntu.com/community/Mailman#Exim4_Configuration
# NB This uses split configuration
# Copy in Mailman files for Exim4
cp -pr ./04_exim4-config_mailman /etc/exim4/conf.d/main/
sed -i "s/lists.example.com/${hostname}/g" /etc/exim4/conf.d/main/04_exim4-config_mailman
cp -pr ./40_exim4-config_mailman /etc/exim4/conf.d/transport/
cp -pr ./101_exim4-config_mailman /etc/exim4/conf.d/router/
# Set dc_use_split_config to true, and ensure dc_other_hostnames has the new listserver hostname
sed -i -r "s/dc_use_split_config.+/dc_use_split_config='true'/" /etc/exim4/update-exim4.conf.conf
# Add hostname to dc_other_hostnames
if [ $(cat /etc/exim4/update-exim4.conf.conf | grep -c "${hostname}") -eq 0 ]; then
        sed -i -E "s/dc_other_hostnames='([^']+)'/dc_other_hostnames='\1:${hostname}'/" /etc/exim4/update-exim4.conf.conf
fi
update-exim4.conf
service exim4 restart
exim -bP '+local_domains'       # Verify config - should show the new listserver hostname

# Set /etc/mailname (may not be necessary, and possibly not desirable if the machine has other mail functions)
#echo $domain > /etc/mailname

# 8 Create a site-wide mailing list
apt-get install -y pwgen
if [ ! -d $prefix/lists/mailman ]; then
	AUTOGENERATED_PASS=`pwgen -c -1 20`
	echo "Site password is: $AUTOGENERATED_PASS"
	$prefix/bin/newlist -q mailman $email $AUTOGENERATED_PASS
fi

# 9 Set up cron; will create entry at /var/spool/cron/crontabs/mailman
crontab -u mailman $prefix/cron/crontab.in

# Create service
cp ./mailman.service /etc/systemd/system/mailman.service
chown root:root /etc/systemd/system/mailman.service
systemctl daemon-reload
systemctl enable mailman.service

# 10 Start the Mailman qrunner
#$prefix/bin/mailmanctl start	# Manual start
systemctl start mailman.service

# Confirm success
echo "Mailman installation complete!"
echo "Mailman is now running."
echo "The site password is shown above."
echo "You can monitor the Mailman error log at: $prefix/logs/error"

# Migration notes
echo ""
echo "If you have existing list archives, they should be copied in to $prefix/archives/"
echo "Then set ownership of that directory throughout to mailman:mailman"
echo ""
echo "To migrate list configs from an old server, export and import using:"
echo "old-server$  config_list -o foo oldlistname"
echo "new-server$  config_list -i foo newlistname"

