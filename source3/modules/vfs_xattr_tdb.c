/*
 * Store posix-level xattrs in a tdb
 *
 * Copyright (C) Volker Lendecke, 2007
 *
 * This program is free software; you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation; either version 3 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program; if not, see <http://www.gnu.org/licenses/>.
 */

#include "includes.h"
#include "system/filesys.h"
#include "smbd/smbd.h"
#include "dbwrap/dbwrap.h"
#include "dbwrap/dbwrap_open.h"
#include "source3/lib/xattr_tdb.h"

#undef DBGC_CLASS
#define DBGC_CLASS DBGC_VFS

static ssize_t xattr_tdb_getxattr(struct vfs_handle_struct *handle,
				  const char *path, const char *name,
				  void *value, size_t size)
{
	SMB_STRUCT_STAT sbuf;
	struct file_id id;
	struct db_context *db;

	SMB_VFS_HANDLE_GET_DATA(handle, db, struct db_context, return -1);

	if (vfs_stat_smb_fname(handle->conn, path, &sbuf) == -1) {
		return -1;
	}

	id = SMB_VFS_FILE_ID_CREATE(handle->conn, &sbuf);

	return xattr_tdb_getattr(db, &id, name, value, size);
}

static ssize_t xattr_tdb_fgetxattr(struct vfs_handle_struct *handle,
				   struct files_struct *fsp,
				   const char *name, void *value, size_t size)
{
	SMB_STRUCT_STAT sbuf;
	struct file_id id;
	struct db_context *db;

	SMB_VFS_HANDLE_GET_DATA(handle, db, struct db_context, return -1);

	if (SMB_VFS_FSTAT(fsp, &sbuf) == -1) {
		return -1;
	}

	id = SMB_VFS_FILE_ID_CREATE(handle->conn, &sbuf);

	return xattr_tdb_getattr(db, &id, name, value, size);
}

static int xattr_tdb_setxattr(struct vfs_handle_struct *handle,
			      const char *path, const char *name,
			      const void *value, size_t size, int flags)
{
	SMB_STRUCT_STAT sbuf;
	struct file_id id;
	struct db_context *db;

	SMB_VFS_HANDLE_GET_DATA(handle, db, struct db_context, return -1);

	if (vfs_stat_smb_fname(handle->conn, path, &sbuf) == -1) {
		return -1;
	}

	id = SMB_VFS_FILE_ID_CREATE(handle->conn, &sbuf);

	return xattr_tdb_setattr(db, &id, name, value, size, flags);
}

static int xattr_tdb_fsetxattr(struct vfs_handle_struct *handle,
			       struct files_struct *fsp,
			       const char *name, const void *value,
			       size_t size, int flags)
{
	SMB_STRUCT_STAT sbuf;
	struct file_id id;
	struct db_context *db;

	SMB_VFS_HANDLE_GET_DATA(handle, db, struct db_context, return -1);

	if (SMB_VFS_FSTAT(fsp, &sbuf) == -1) {
		return -1;
	}

	id = SMB_VFS_FILE_ID_CREATE(handle->conn, &sbuf);

	return xattr_tdb_setattr(db, &id, name, value, size, flags);
}

static ssize_t xattr_tdb_listxattr(struct vfs_handle_struct *handle,
				   const char *path, char *list, size_t size)
{
	SMB_STRUCT_STAT sbuf;
	struct file_id id;
	struct db_context *db;

	SMB_VFS_HANDLE_GET_DATA(handle, db, struct db_context, return -1);

	if (vfs_stat_smb_fname(handle->conn, path, &sbuf) == -1) {
		return -1;
	}

	id = SMB_VFS_FILE_ID_CREATE(handle->conn, &sbuf);

	return xattr_tdb_listattr(db, &id, list, size);
}

static ssize_t xattr_tdb_flistxattr(struct vfs_handle_struct *handle,
				    struct files_struct *fsp, char *list,
				    size_t size)
{
	SMB_STRUCT_STAT sbuf;
	struct file_id id;
	struct db_context *db;

	SMB_VFS_HANDLE_GET_DATA(handle, db, struct db_context, return -1);

	if (SMB_VFS_FSTAT(fsp, &sbuf) == -1) {
		return -1;
	}

	id = SMB_VFS_FILE_ID_CREATE(handle->conn, &sbuf);

	return xattr_tdb_listattr(db, &id, list, size);
}

static int xattr_tdb_removexattr(struct vfs_handle_struct *handle,
				 const char *path, const char *name)
{
	SMB_STRUCT_STAT sbuf;
	struct file_id id;
	struct db_context *db;

	SMB_VFS_HANDLE_GET_DATA(handle, db, struct db_context, return -1);

	if (vfs_stat_smb_fname(handle->conn, path, &sbuf) == -1) {
		return -1;
	}

	id = SMB_VFS_FILE_ID_CREATE(handle->conn, &sbuf);

	return xattr_tdb_removeattr(db, &id, name);
}

static int xattr_tdb_fremovexattr(struct vfs_handle_struct *handle,
				  struct files_struct *fsp, const char *name)
{
	SMB_STRUCT_STAT sbuf;
	struct file_id id;
	struct db_context *db;

	SMB_VFS_HANDLE_GET_DATA(handle, db, struct db_context, return -1);

	if (SMB_VFS_FSTAT(fsp, &sbuf) == -1) {
		return -1;
	}

	id = SMB_VFS_FILE_ID_CREATE(handle->conn, &sbuf);

	return xattr_tdb_removeattr(db, &id, name);
}

/*
 * Open the tdb file upon VFS_CONNECT
 */

static bool xattr_tdb_init(int snum, struct db_context **p_db)
{
	struct db_context *db;
	const char *dbname;
	char *def_dbname;

	def_dbname = state_path("xattr.tdb");
	if (def_dbname == NULL) {
		errno = ENOSYS;
		return false;
	}

	dbname = lp_parm_const_string(snum, "xattr_tdb", "file", def_dbname);

	/* now we know dbname is not NULL */

	become_root();
	db = db_open(NULL, dbname, 0, TDB_DEFAULT, O_RDWR|O_CREAT, 0600,
		     DBWRAP_LOCK_ORDER_2);
	unbecome_root();

	if (db == NULL) {
#if defined(ENOTSUP)
		errno = ENOTSUP;
#else
		errno = ENOSYS;
#endif
		TALLOC_FREE(def_dbname);
		return false;
	}

	*p_db = db;
	TALLOC_FREE(def_dbname);
	return true;
}

/*
 * On unlink we need to delete the tdb record
 */
static int xattr_tdb_unlink(vfs_handle_struct *handle,
			    const struct smb_filename *smb_fname)
{
	struct smb_filename *smb_fname_tmp = NULL;
	struct file_id id;
	struct db_context *db;
	NTSTATUS status;
	int ret = -1;
	bool remove_record = false;

	SMB_VFS_HANDLE_GET_DATA(handle, db, struct db_context, return -1);

	status = copy_smb_filename(talloc_tos(), smb_fname, &smb_fname_tmp);
	if (!NT_STATUS_IS_OK(status)) {
		errno = map_errno_from_nt_status(status);
		return -1;
	}

	if (lp_posix_pathnames()) {
		ret = SMB_VFS_LSTAT(handle->conn, smb_fname_tmp);
	} else {
		ret = SMB_VFS_STAT(handle->conn, smb_fname_tmp);
	}
	if (ret == -1) {
		goto out;
	}

	if (smb_fname_tmp->st.st_ex_nlink == 1) {
		/* Only remove record on last link to file. */
		remove_record = true;
	}

	ret = SMB_VFS_NEXT_UNLINK(handle, smb_fname_tmp);

	if (ret == -1) {
		goto out;
	}

	if (!remove_record) {
		goto out;
	}

	id = SMB_VFS_FILE_ID_CREATE(handle->conn, &smb_fname_tmp->st);

	xattr_tdb_remove_all_attrs(db, &id);

 out:
	TALLOC_FREE(smb_fname_tmp);
	return ret;
}

/*
 * On rmdir we need to delete the tdb record
 */
static int xattr_tdb_rmdir(vfs_handle_struct *handle, const char *path)
{
	SMB_STRUCT_STAT sbuf;
	struct file_id id;
	struct db_context *db;
	int ret;

	SMB_VFS_HANDLE_GET_DATA(handle, db, struct db_context, return -1);

	if (vfs_stat_smb_fname(handle->conn, path, &sbuf) == -1) {
		return -1;
	}

	ret = SMB_VFS_NEXT_RMDIR(handle, path);

	if (ret == -1) {
		return -1;
	}

	id = SMB_VFS_FILE_ID_CREATE(handle->conn, &sbuf);

	xattr_tdb_remove_all_attrs(db, &id);

	return 0;
}

/*
 * Destructor for the VFS private data
 */

static void close_xattr_db(void **data)
{
	struct db_context **p_db = (struct db_context **)data;
	TALLOC_FREE(*p_db);
}

static int xattr_tdb_connect(vfs_handle_struct *handle, const char *service,
			  const char *user)
{
	char *sname = NULL;
	int res, snum;
	struct db_context *db;

	res = SMB_VFS_NEXT_CONNECT(handle, service, user);
	if (res < 0) {
		return res;
	}

	snum = find_service(talloc_tos(), service, &sname);
	if (snum == -1 || sname == NULL) {
		/*
		 * Should not happen, but we should not fail just *here*.
		 */
		return 0;
	}

	if (!xattr_tdb_init(snum, &db)) {
		DEBUG(5, ("Could not init xattr tdb\n"));
		lp_do_parameter(snum, "ea support", "False");
		return 0;
	}

	lp_do_parameter(snum, "ea support", "True");

	SMB_VFS_HANDLE_SET_DATA(handle, db, close_xattr_db,
				struct db_context, return -1);

	return 0;
}

static struct vfs_fn_pointers vfs_xattr_tdb_fns = {
	.getxattr_fn = xattr_tdb_getxattr,
	.fgetxattr_fn = xattr_tdb_fgetxattr,
	.setxattr_fn = xattr_tdb_setxattr,
	.fsetxattr_fn = xattr_tdb_fsetxattr,
	.listxattr_fn = xattr_tdb_listxattr,
	.flistxattr_fn = xattr_tdb_flistxattr,
	.removexattr_fn = xattr_tdb_removexattr,
	.fremovexattr_fn = xattr_tdb_fremovexattr,
	.unlink_fn = xattr_tdb_unlink,
	.rmdir_fn = xattr_tdb_rmdir,
	.connect_fn = xattr_tdb_connect,
};

NTSTATUS vfs_xattr_tdb_init(void);
NTSTATUS vfs_xattr_tdb_init(void)
{
	return smb_register_vfs(SMB_VFS_INTERFACE_VERSION, "xattr_tdb",
				&vfs_xattr_tdb_fns);
}
