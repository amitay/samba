# backend code for upgrading from Samba3
# Copyright Jelmer Vernooij 2005-2007
# Copyright Andrew Bartlett 2011
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

"""Support code for upgrading from Samba 3 to Samba 4."""

__docformat__ = "restructuredText"

import ldb
import time
import pwd

from samba import Ldb, registry
from samba.param import LoadParm
from samba.provision import provision, FILL_FULL, ProvisioningError
from samba.samba3 import passdb
from samba.samba3 import param as s3param
from samba.dcerpc import lsa, samr, security
from samba.dcerpc.security import dom_sid
from samba import dsdb
from samba.ndr import ndr_pack
from samba import unix2nttime


def import_sam_policy(samdb, policy, logger):
    """Import a Samba 3 policy.

    :param samdb: Samba4 SAM database
    :param policy: Samba3 account policy
    :param logger: Logger object
    """

    # Following entries are used -
    #    min password length, password history, minimum password age,
    #    maximum password age, lockout duration
    #
    # Following entries are not used -
    #    reset count minutes, user must logon to change password,
    #    bad lockout minutes, disconnect time

    m = ldb.Message()
    m.dn = samdb.get_default_basedn()
    m['a01'] = ldb.MessageElement(str(policy['min password length']),
        ldb.FLAG_MOD_REPLACE, 'minPwdLength')
    m['a02'] = ldb.MessageElement(str(policy['password history']),
        ldb.FLAG_MOD_REPLACE, 'pwdHistoryLength')

    min_pw_age_unix = policy['minimum password age']
    min_pw_age_nt = int(-min_pw_age_unix * (1e7 * 60 * 60 * 24))
    m['a03'] = ldb.MessageElement(str(min_pw_age_nt), ldb.FLAG_MOD_REPLACE,
        'minPwdAge')

    max_pw_age_unix = policy['maximum password age']
    if max_pw_age_unix == -1 or max_pw_age_unix == 0:
        max_pw_age_nt = -0x8000000000000000
    else:
        max_pw_age_nt = int(-max_pw_age_unix * (1e7 * 60 * 60 * 24))

    m['a04'] = ldb.MessageElement(str(max_pw_age_nt), ldb.FLAG_MOD_REPLACE,
                                  'maxPwdAge')

    lockout_duration_mins = policy['lockout duration']
    lockout_duration_nt = unix2nttime(lockout_duration_mins * 60)

    m['a05'] = ldb.MessageElement(str(lockout_duration_nt),
        ldb.FLAG_MOD_REPLACE, 'lockoutDuration')

    try:
        samdb.modify(m)
    except ldb.LdbError, e:
        logger.warn("Could not set account policy, (%s)", str(e))


def add_idmap_entry(idmapdb, sid, xid, xid_type, logger):
    """Create idmap entry

    :param idmapdb: Samba4 IDMAP database
    :param sid: user/group sid
    :param xid: user/group id
    :param xid_type: type of id (ID_TYPE_UID/ID_TYPE_GID)
    :param logger: Logger object
    """

    # First try to see if we already have this entry
    found = False
    msg = idmapdb.search(expression='objectSid=%s' % str(sid))
    if msg.count == 1:
        found = True

    if found:
        try:
            m = ldb.Message()
            m.dn = msg[0]['dn']
            m['xidNumber'] = ldb.MessageElement(
                str(xid), ldb.FLAG_MOD_REPLACE, 'xidNumber')
            m['type'] = ldb.MessageElement(
                xid_type, ldb.FLAG_MOD_REPLACE, 'type')
            idmapdb.modify(m)
        except ldb.LdbError, e:
            logger.warn(
                'Could not modify idmap entry for sid=%s, id=%s, type=%s (%s)',
                str(sid), str(xid), xid_type, str(e))
    else:
        try:
            idmapdb.add({"dn": "CN=%s" % str(sid),
                        "cn": str(sid),
                        "objectClass": "sidMap",
                        "objectSid": ndr_pack(sid),
                        "type": xid_type,
                        "xidNumber": str(xid)})
        except ldb.LdbError, e:
            logger.warn(
                'Could not add idmap entry for sid=%s, id=%s, type=%s (%s)',
                str(sid), str(xid), xid_type, str(e))


def import_idmap(idmapdb, samba3, logger):
    """Import idmap data.

    :param idmapdb: Samba4 IDMAP database
    :param samba3_idmap: Samba3 IDMAP database to import from
    :param logger: Logger object
    """

    try:
        samba3_idmap = samba3.get_idmap_db()
    except IOError, e:
        logger.warn('Cannot open idmap database, Ignoring: %s', str(e))
        return

    currentxid = max(samba3_idmap.get_user_hwm(), samba3_idmap.get_group_hwm())
    lowerbound = currentxid
    # FIXME: upperbound

    m = ldb.Message()
    m.dn = ldb.Dn(idmapdb, 'CN=CONFIG')
    m['lowerbound'] = ldb.MessageElement(
        str(lowerbound), ldb.FLAG_MOD_REPLACE, 'lowerBound')
    m['xidNumber'] = ldb.MessageElement(
        str(currentxid), ldb.FLAG_MOD_REPLACE, 'xidNumber')
    idmapdb.modify(m)

    for id_type, xid in samba3_idmap.ids():
        if id_type == 'UID':
            xid_type = 'ID_TYPE_UID'
        elif id_type == 'GID':
            xid_type = 'ID_TYPE_GID'
        else:
            logger.warn('Wrong type of entry in idmap (%s), Ignoring', id_type)
            continue

        sid = samba3_idmap.get_sid(xid, id_type)
        add_idmap_entry(idmapdb, dom_sid(sid), xid, xid_type, logger)


def add_group_from_mapping_entry(samdb, groupmap, logger):
    """Add or modify group from group mapping entry

    param samdb: Samba4 SAM database
    param groupmap: Groupmap entry
    param logger: Logger object
    """

    # First try to see if we already have this entry
    try:
        msg = samdb.search(
            base='<SID=%s>' % str(groupmap.sid), scope=ldb.SCOPE_BASE)
        found = True
    except ldb.LdbError, (ecode, emsg):
        if ecode == ldb.ERR_NO_SUCH_OBJECT:
            found = False
        else:
            raise ldb.LdbError(ecode, emsg)

    if found:
        logger.warn('Group already exists sid=%s, groupname=%s existing_groupname=%s, Ignoring.',
                            str(groupmap.sid), groupmap.nt_name, msg[0]['sAMAccountName'][0])
    else:
        if groupmap.sid_name_use == lsa.SID_NAME_WKN_GRP:
            # In a lot of Samba3 databases, aliases are marked as well known groups
            (group_dom_sid, rid) = groupmap.sid.split()
            if (group_dom_sid != security.dom_sid(security.SID_BUILTIN)):
                return

        m = ldb.Message()
        m.dn = ldb.Dn(samdb, "CN=%s,CN=Users,%s" % (groupmap.nt_name, samdb.get_default_basedn()))
        m['cn'] = ldb.MessageElement(groupmap.nt_name, ldb.FLAG_MOD_ADD, 'cn')
        m['objectClass'] = ldb.MessageElement('group', ldb.FLAG_MOD_ADD, 'objectClass')
        m['objectSid'] = ldb.MessageElement(ndr_pack(groupmap.sid), ldb.FLAG_MOD_ADD,
            'objectSid')
        m['sAMAccountName'] = ldb.MessageElement(groupmap.nt_name, ldb.FLAG_MOD_ADD,
            'sAMAccountName')

        if groupmap.comment:
            m['description'] = ldb.MessageElement(groupmap.comment, ldb.FLAG_MOD_ADD,
                'description')

        # Fix up incorrect 'well known' groups that are actually builtin (per test above) to be aliases
        if groupmap.sid_name_use == lsa.SID_NAME_ALIAS or groupmap.sid_name_use == lsa.SID_NAME_WKN_GRP:
            m['groupType'] = ldb.MessageElement(str(dsdb.GTYPE_SECURITY_DOMAIN_LOCAL_GROUP),
                ldb.FLAG_MOD_ADD, 'groupType')

        try:
            samdb.add(m, controls=["relax:0"])
        except ldb.LdbError, e:
            logger.warn('Could not add group name=%s (%s)', groupmap.nt_name, str(e))


def add_users_to_group(samdb, group, members, logger):
    """Add user/member to group/alias

    param samdb: Samba4 SAM database
    param group: Groupmap object
    param members: List of member SIDs
    param logger: Logger object
    """
    for member_sid in members:
        m = ldb.Message()
        m.dn = ldb.Dn(samdb, "<SID=%s>" % str(group.sid))
        m['a01'] = ldb.MessageElement("<SID=%s>" % str(member_sid), ldb.FLAG_MOD_ADD, 'member')

        try:
            samdb.modify(m)
        except ldb.LdbError, (ecode, emsg):
            if ecode == ldb.ERR_ENTRY_ALREADY_EXISTS:
                logger.debug("skipped re-adding member '%s' to group '%s': %s", member_sid, group.sid, emsg)
            elif ecode == ldb.ERR_NO_SUCH_OBJECT:
                raise ProvisioningError("Could not add member '%s' to group '%s' as either group or user record doesn't exist: %s" % (member_sid, group.sid, emsg))
            else:
                raise ProvisioningError("Could not add member '%s' to group '%s': %s" % (member_sid, group.sid, emsg))


def import_wins(samba4_winsdb, samba3_winsdb):
    """Import settings from a Samba3 WINS database.

    :param samba4_winsdb: WINS database to import to
    :param samba3_winsdb: WINS database to import from
    """

    version_id = 0

    for (name, (ttl, ips, nb_flags)) in samba3_winsdb.items():
        version_id += 1

        type = int(name.split("#", 1)[1], 16)

        if type == 0x1C:
            rType = 0x2
        elif type & 0x80:
            if len(ips) > 1:
                rType = 0x2
            else:
                rType = 0x1
        else:
            if len(ips) > 1:
                rType = 0x3
            else:
                rType = 0x0

        if ttl > time.time():
            rState = 0x0 # active
        else:
            rState = 0x1 # released

        nType = ((nb_flags & 0x60) >> 5)

        samba4_winsdb.add({"dn": "name=%s,type=0x%s" % tuple(name.split("#")),
                           "type": name.split("#")[1],
                           "name": name.split("#")[0],
                           "objectClass": "winsRecord",
                           "recordType": str(rType),
                           "recordState": str(rState),
                           "nodeType": str(nType),
                           "expireTime": ldb.timestring(ttl),
                           "isStatic": "0",
                           "versionID": str(version_id),
                           "address": ips})

    samba4_winsdb.add({"dn": "cn=VERSION",
                       "cn": "VERSION",
                       "objectClass": "winsMaxVersion",
                       "maxVersion": str(version_id)})


def enable_samba3sam(samdb, ldapurl):
    """Enable Samba 3 LDAP URL database.

    :param samdb: SAM Database.
    :param ldapurl: Samba 3 LDAP URL
    """
    samdb.modify_ldif("""
dn: @MODULES
changetype: modify
replace: @LIST
@LIST: samldb,operational,objectguid,rdn_name,samba3sam
""")

    samdb.add({"dn": "@MAP=samba3sam", "@MAP_URL": ldapurl})


smbconf_keep = [
    "dos charset",
    "unix charset",
    "display charset",
    "comment",
    "path",
    "directory",
    "workgroup",
    "realm",
    "netbios name",
    "netbios aliases",
    "netbios scope",
    "server string",
    "interfaces",
    "bind interfaces only",
    "security",
    "auth methods",
    "encrypt passwords",
    "null passwords",
    "obey pam restrictions",
    "password server",
    "smb passwd file",
    "private dir",
    "passwd chat",
    "password level",
    "lanman auth",
    "ntlm auth",
    "client NTLMv2 auth",
    "client lanman auth",
    "client plaintext auth",
    "read only",
    "hosts allow",
    "hosts deny",
    "log level",
    "debuglevel",
    "log file",
    "smb ports",
    "large readwrite",
    "max protocol",
    "min protocol",
    "unicode",
    "read raw",
    "write raw",
    "disable netbios",
    "nt status support",
    "max mux",
    "max xmit",
    "name resolve order",
    "max wins ttl",
    "min wins ttl",
    "time server",
    "unix extensions",
    "use spnego",
    "server signing",
    "client signing",
    "max connections",
    "paranoid server security",
    "socket options",
    "strict sync",
    "max print jobs",
    "printable",
    "print ok",
    "printer name",
    "printer",
    "map system",
    "map hidden",
    "map archive",
    "preferred master",
    "prefered master",
    "local master",
    "browseable",
    "browsable",
    "wins server",
    "wins support",
    "csc policy",
    "strict locking",
    "preload",
    "auto services",
    "lock dir",
    "lock directory",
    "pid directory",
    "socket address",
    "copy",
    "include",
    "available",
    "volume",
    "fstype",
    "panic action",
    "msdfs root",
    "host msdfs",
    "winbind separator"]


def upgrade_smbconf(oldconf, mark):
    """Remove configuration variables not present in Samba4

    :param oldconf: Old configuration structure
    :param mark: Whether removed configuration variables should be
        kept in the new configuration as "samba3:<name>"
    """
    data = oldconf.data()
    newconf = LoadParm()

    for s in data:
        for p in data[s]:
            keep = False
            for k in smbconf_keep:
                if smbconf_keep[k] == p:
                    keep = True
                    break

            if keep:
                newconf.set(s, p, oldconf.get(s, p))
            elif mark:
                newconf.set(s, "samba3:" + p, oldconf.get(s, p))

    return newconf

SAMBA3_PREDEF_NAMES = {
        'HKLM': registry.HKEY_LOCAL_MACHINE,
}


def import_registry(samba4_registry, samba3_regdb):
    """Import a Samba 3 registry database into the Samba 4 registry.

    :param samba4_registry: Samba 4 registry handle.
    :param samba3_regdb: Samba 3 registry database handle.
    """
    def ensure_key_exists(keypath):
        (predef_name, keypath) = keypath.split("/", 1)
        predef_id = SAMBA3_PREDEF_NAMES[predef_name]
        keypath = keypath.replace("/", "\\")
        return samba4_registry.create_key(predef_id, keypath)

    for key in samba3_regdb.keys():
        key_handle = ensure_key_exists(key)
        for subkey in samba3_regdb.subkeys(key):
            ensure_key_exists(subkey)
        for (value_name, (value_type, value_data)) in samba3_regdb.values(key).items():
            key_handle.set_value(value_name, value_type, value_data)


def upgrade_from_samba3(samba3, logger, targetdir, session_info=None, useeadb=False):
    """Upgrade from samba3 database to samba4 AD database

    :param samba3: samba3 object
    :param logger: Logger object
    :param targetdir: samba4 database directory
    :param session_info: Session information
    """
    serverrole = samba3.lp.server_role()

    domainname = samba3.lp.get("workgroup")
    realm = samba3.lp.get("realm")
    netbiosname = samba3.lp.get("netbios name")

    # secrets db
    try:
        secrets_db = samba3.get_secrets_db()
    except IOError, e:
        raise ProvisioningError("Could not open '%s', the Samba3 secrets database: %s.  Perhaps you specified the incorrect smb.conf, --testparm or --dbdir option?" % (samba3.privatedir_path("secrets.tdb"), str(e)))

    if not domainname:
        domainname = secrets_db.domains()[0]
        logger.warning("No workgroup specified in smb.conf file, assuming '%s'",
                domainname)

    if not realm:
        if serverrole == "ROLE_DOMAIN_BDC" or serverrole == "ROLE_DOMAIN_PDC":
            raise ProvisioningError("No realm specified in smb.conf file and being a DC. That upgrade path doesn't work! Please add a 'realm' directive to your old smb.conf to let us know which one you want to use (it is the DNS name of the AD domain you wish to create.")
        else:
            realm = domainname.upper()
            logger.warning("No realm specified in smb.conf file, assuming '%s'",
                    realm)

    # Find machine account and password
    next_rid = 1000

    try:
        machinepass = secrets_db.get_machine_password(netbiosname)
    except KeyError:
        machinepass = None

    # We must close the direct pytdb database before the C code loads it
    secrets_db.close()

    # Connect to old password backend
    passdb.set_secrets_dir(samba3.lp.get("private dir"))
    s3db = samba3.get_sam_db()

    # Get domain sid
    try:
        domainsid = passdb.get_global_sam_sid()
    except passdb.error:
        raise Exception("Can't find domain sid for '%s', Exiting." % domainname)

    # Get machine account, sid, rid
    try:
        machineacct = s3db.getsampwnam('%s$' % netbiosname)
    except passdb.error:
        machinerid = None
        machinesid = None
    else:
        machinesid, machinerid = machineacct.user_sid.split()

    # Export account policy
    logger.info("Exporting account policy")
    policy = s3db.get_account_policy()

    # Export groups from old passdb backend
    logger.info("Exporting groups")
    grouplist = s3db.enum_group_mapping()
    groupmembers = {}
    for group in grouplist:
        sid, rid = group.sid.split()
        if sid == domainsid:
            if rid >= next_rid:
                next_rid = rid + 1

        # Get members for each group/alias
        if group.sid_name_use == lsa.SID_NAME_ALIAS:
            try:
                members = s3db.enum_aliasmem(group.sid)
            except passdb.error:
                logger.warn("Ignoring group '%s' %s listed but then not found: %s",
                            group.nt_name, group.sid, passdb.error)
                continue
        elif group.sid_name_use == lsa.SID_NAME_DOM_GRP:
            try:
                members = s3db.enum_group_members(group.sid)
            except passdb.error:
                logger.warn("Ignoring group '%s' %s listed but then not found: %s",
                            group.nt_name, group.sid, passdb.error)
                continue
            groupmembers[group.nt_name] = members
        elif group.sid_name_use == lsa.SID_NAME_WKN_GRP:
            (group_dom_sid, rid) = group.sid.split()
            if (group_dom_sid != security.dom_sid(security.SID_BUILTIN)):
                logger.warn("Ignoring 'well known' group '%s' (should already be in AD, and have no members)",
                            group.nt_name)
                continue
            # A number of buggy databases mix up well known groups and aliases.
            try:
                members = s3db.enum_aliasmem(group.sid)
            except passdb.error:
                logger.warn("Ignoring group '%s' %s listed but then not found: %s",
                            group.nt_name, group.sid, passdb.error)
                continue
        else:
            logger.warn("Ignoring group '%s' with sid_name_use=%d",
                        group.nt_name, group.sid_name_use)
            continue

    # Export users from old passdb backend
    logger.info("Exporting users")
    userlist = s3db.search_users(0)
    userdata = {}
    uids = {}
    admin_user = None
    for entry in userlist:
        if machinerid and machinerid == entry['rid']:
            continue
        username = entry['account_name']
        if entry['rid'] < 1000:
            logger.info("  Skipping wellknown rid=%d (for username=%s)", entry['rid'], username)
            continue
        if entry['rid'] >= next_rid:
            next_rid = entry['rid'] + 1

        user = s3db.getsampwnam(username)
        acct_type = (user.acct_ctrl & (samr.ACB_NORMAL|samr.ACB_WSTRUST|samr.ACB_SVRTRUST|samr.ACB_DOMTRUST))
        if (acct_type == samr.ACB_NORMAL or acct_type == samr.ACB_WSTRUST or acct_type == samr.ACB_SVRTRUST):
            pass
        elif acct_type == samr.ACB_DOMTRUST:
            logger.warn("  Skipping inter-domain trust from domain %s, this trust must be re-created as an AD trust" % username[:-1])
            continue
        elif acct_type == (samr.ACB_NORMAL|samr.ACB_WSTRUST) and username[-1] == '$':
            logger.warn("  Fixing account %s which had both ACB_NORMAL (U) and ACB_WSTRUST (W) set.  Account will be marked as ACB_WSTRUST (W), i.e. as a domain member" % username)
            user.acct_ctrl = (user.acct_ctrl & ~samr.ACB_NORMAL)
        else:
            raise ProvisioningError("""Failed to upgrade due to invalid account %s, account control flags 0x%08X must have exactly one of
ACB_NORMAL (N, 0x%08X), ACB_WSTRUST (W 0x%08X), ACB_SVRTRUST (S 0x%08X) or ACB_DOMTRUST (D 0x%08X).

Please fix this account before attempting to upgrade again
"""
                                    % (user.acct_flags, username,
                                       samr.ACB_NORMAL, samr.ACB_WSTRUST, samr.ACB_SVRTRUST, samr.ACB_DOMTRUST))

        userdata[username] = user
        try:
            uids[username] = s3db.sid_to_id(user.user_sid)[0]
        except passdb.error:
            try:
                uids[username] = pwd.getpwnam(username).pw_uid
            except KeyError:
                pass

        if not admin_user and username.lower() == 'root':
            admin_user = username
        if username.lower() == 'administrator':
            admin_user = username

    logger.info("Next rid = %d", next_rid)

    # Check for same username/groupname
    group_names = set([g.nt_name for g in grouplist])
    user_names = set([u['account_name'] for u in userlist])
    common_names = group_names.intersection(user_names)
    if common_names:
        logger.error("Following names are both user names and group names:")
        for name in common_names:
            logger.error("   %s" % name)
        raise ProvisioningError("Please remove common user/group names before upgrade.")

    # Check for same user sid/group sid
    group_sids = set([str(g.sid) for g in grouplist])
    if len(grouplist) != len(group_sids):
        raise ProvisioningError("Please remove duplicate group sid entries before upgrade.")
    user_sids = set(["%s-%u" % (domainsid, u['rid']) for u in userlist])
    if len(userlist) != len(user_sids):
        raise ProvisioningError("Please remove duplicate user sid entries before upgrade.")
    common_sids = group_sids.intersection(user_sids)
    if common_sids:
        logger.error("Following sids are both user and group sids:")
        for sid in common_sids:
            logger.error("   %s" % str(sid))
        raise ProvisioningError("Please remove duplicate sid entries before upgrade.")

    if serverrole == "ROLE_DOMAIN_BDC" or serverrole == "ROLE_DOMAIN_PDC":
        dns_backend = "BIND9_DLZ"
    else:
        dns_backend = "NONE"

    # Do full provision
    result = provision(logger, session_info, None,
                       targetdir=targetdir, realm=realm, domain=domainname,
                       domainsid=str(domainsid), next_rid=next_rid,
                       dc_rid=machinerid,
                       dom_for_fun_level=dsdb.DS_DOMAIN_FUNCTION_2003,
                       hostname=netbiosname.lower(), machinepass=machinepass,
                       serverrole=serverrole, samdb_fill=FILL_FULL,
                       useeadb=useeadb, dns_backend=dns_backend)
    result.report_logger(logger)

    # Import WINS database
    logger.info("Importing WINS database")

    samba3_winsdb = None
    try:
        samba3_winsdb = samba3.get_wins_db()
    except IOError, e:
        logger.warn('Cannot open wins database, Ignoring: %s', str(e))

    if samba3_winsdb:
        import_wins(Ldb(result.paths.winsdb), samba3_winsdb)

    # Set Account policy
    logger.info("Importing Account policy")
    import_sam_policy(result.samdb, policy, logger)

    # Migrate IDMAP database
    logger.info("Importing idmap database")
    import_idmap(result.idmap, samba3, logger)

    # Set the s3 context for samba4 configuration
    new_lp_ctx = s3param.get_context()
    new_lp_ctx.load(result.lp.configfile)
    new_lp_ctx.set("private dir", result.lp.get("private dir"))
    new_lp_ctx.set("state directory", result.lp.get("state directory"))
    new_lp_ctx.set("lock directory", result.lp.get("lock directory"))

    # Connect to samba4 backend
    s4_passdb = passdb.PDB(new_lp_ctx.get("passdb backend"))

    # Export groups to samba4 backend
    logger.info("Importing groups")
    for g in grouplist:
        # Ignore uninitialized groups (gid = -1)
        if g.gid != -1:
            add_idmap_entry(result.idmap, g.sid, g.gid, "ID_TYPE_GID", logger)
            add_group_from_mapping_entry(result.samdb, g, logger)

    # Export users to samba4 backend
    logger.info("Importing users")
    for username in userdata:
        if username.lower() == 'administrator' or username.lower() == 'root':
            continue
        s4_passdb.add_sam_account(userdata[username])
        if username in uids:
            add_idmap_entry(result.idmap, userdata[username].user_sid, uids[username], "ID_TYPE_UID", logger)

    logger.info("Adding users to groups")
    for g in grouplist:
        if g.nt_name in groupmembers:
            add_users_to_group(result.samdb, g, groupmembers[g.nt_name], logger)

    # Set password for administrator
    if admin_user:
        logger.info("Setting password for administrator")
        admin_userdata = s4_passdb.getsampwnam("administrator")
        admin_userdata.nt_passwd = userdata[admin_user].nt_passwd
        if userdata[admin_user].lanman_passwd:
            admin_userdata.lanman_passwd = userdata[admin_user].lanman_passwd
        admin_userdata.pass_last_set_time = userdata[admin_user].pass_last_set_time
        if userdata[admin_user].pw_history:
            admin_userdata.pw_history = userdata[admin_user].pw_history
        s4_passdb.update_sam_account(admin_userdata)
        logger.info("Administrator password has been set to password of user '%s'", admin_user)

    # FIXME: import_registry(registry.Registry(), samba3.get_registry())
    # FIXME: shares
