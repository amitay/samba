/*
   Unix SMB/CIFS implementation.
   Cache db contents for parse_record based on seqnum
   Copyright (C) Volker Lendecke 2012

   This program is free software; you can redistribute it and/or modify
   it under the terms of the GNU General Public License as published by
   the Free Software Foundation; either version 3 of the License, or
   (at your option) any later version.

   This program is distributed in the hope that it will be useful,
   but WITHOUT ANY WARRANTY; without even the implied warranty of
   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
   GNU General Public License for more details.

   You should have received a copy of the GNU General Public License
   along with this program.  If not, see <http://www.gnu.org/licenses/>.
*/

#include "includes.h"
#include "lib/dbwrap/dbwrap.h"
#include "lib/dbwrap/dbwrap_private.h"
#include "lib/dbwrap/dbwrap_rbt.h"
#include "lib/dbwrap/dbwrap_cache.h"

struct db_cache_ctx {
	int seqnum;
	struct db_context *backing;
	struct db_context *positive;
	struct db_context *negative;
};

static void dbwrap_cache_validate(struct db_cache_ctx *ctx)
{
	if (ctx->seqnum == dbwrap_get_seqnum(ctx->backing)) {
		return;
	}
	TALLOC_FREE(ctx->positive);
	ctx->positive = db_open_rbt(ctx);
	TALLOC_FREE(ctx->negative);
	ctx->negative = db_open_rbt(ctx);
}

static NTSTATUS dbwrap_cache_parse_record(
	struct db_context *db, TDB_DATA key,
	void (*parser)(TDB_DATA key, TDB_DATA data, void *private_data),
	void *private_data)
{
	struct db_cache_ctx *ctx = talloc_get_type_abort(
		db->private_data, struct db_cache_ctx);
	TDB_DATA value;
	NTSTATUS status;

	dbwrap_cache_validate(ctx);

	if (ctx->positive != NULL) {
		status = dbwrap_parse_record(
			ctx->positive, key, parser, private_data);
		if (NT_STATUS_IS_OK(status)) {
			return status;
		}
	}
	if ((ctx->negative != NULL) && dbwrap_exists(ctx->negative, key)) {
		return NT_STATUS_NOT_FOUND;
	}

	status = dbwrap_fetch(ctx->backing, talloc_tos(), key, &value);

	if (NT_STATUS_IS_OK(status)) {
		dbwrap_store(ctx->positive, key, value, 0);
		parser(key, value, private_data);
		TALLOC_FREE(value.dptr);
		return NT_STATUS_OK;
	}

	if (NT_STATUS_EQUAL(status, NT_STATUS_NOT_FOUND)) {
		char c = '\0';
		value.dptr = (uint8_t *)&c;
		value.dsize = sizeof(c);
		dbwrap_store(ctx->negative, key, value, 0);
		return NT_STATUS_NOT_FOUND;
	}
	return status;
}

static struct db_record *dbwrap_cache_fetch_locked(
	struct db_context *db, TALLOC_CTX *mem_ctx, TDB_DATA key)
{
	struct db_cache_ctx *ctx = talloc_get_type_abort(
		db->private_data, struct db_cache_ctx);
	return dbwrap_fetch_locked(ctx->backing, mem_ctx, key);
}

static int dbwrap_cache_traverse(struct db_context *db,
				 int (*f)(struct db_record *rec,
					  void *private_data),
				 void *private_data)
{
	struct db_cache_ctx *ctx = talloc_get_type_abort(
		db->private_data, struct db_cache_ctx);
	NTSTATUS status;
	int ret;
	status = dbwrap_traverse(ctx->backing, f, private_data, &ret);
	if (!NT_STATUS_IS_OK(status)) {
		return -1;
	}
	return ret;
}

static int dbwrap_cache_traverse_read(struct db_context *db,
				      int (*f)(struct db_record *rec,
					       void *private_data),
				      void *private_data)
{
	struct db_cache_ctx *ctx = talloc_get_type_abort(
		db->private_data, struct db_cache_ctx);
	NTSTATUS status;
	int ret;
	status = dbwrap_traverse_read(ctx->backing, f, private_data, &ret);
	if (!NT_STATUS_IS_OK(status)) {
		return -1;
	}
	return ret;
}

static int dbwrap_cache_get_seqnum(struct db_context *db)
{
	struct db_cache_ctx *ctx = talloc_get_type_abort(
		db->private_data, struct db_cache_ctx);
	return dbwrap_get_seqnum(ctx->backing);
}

static int dbwrap_cache_get_flags(struct db_context *db)
{
	struct db_cache_ctx *ctx = talloc_get_type_abort(
		db->private_data, struct db_cache_ctx);
	return dbwrap_get_flags(ctx->backing);
}

static int dbwrap_cache_transaction_start(struct db_context *db)
{
	struct db_cache_ctx *ctx = talloc_get_type_abort(
		db->private_data, struct db_cache_ctx);
	return dbwrap_transaction_start(ctx->backing);
}

static int dbwrap_cache_transaction_commit(struct db_context *db)
{
	struct db_cache_ctx *ctx = talloc_get_type_abort(
		db->private_data, struct db_cache_ctx);
	return dbwrap_transaction_commit(ctx->backing);
}

static int dbwrap_cache_transaction_cancel(struct db_context *db)
{
	struct db_cache_ctx *ctx = talloc_get_type_abort(
		db->private_data, struct db_cache_ctx);
	return dbwrap_transaction_cancel(ctx->backing);
}

static int dbwrap_cache_exists(struct db_context *db, TDB_DATA key)
{
	struct db_cache_ctx *ctx = talloc_get_type_abort(
		db->private_data, struct db_cache_ctx);

	if (ctx->positive && dbwrap_exists(ctx->positive, key)) {
		return true;
	}
	if (ctx->negative && dbwrap_exists(ctx->negative, key)) {
		return false;
	}
	return dbwrap_exists(ctx->backing, key);
}

struct db_context *db_open_cache(TALLOC_CTX *mem_ctx,
				 struct db_context *backing)
{
	struct db_context *db;
	struct db_cache_ctx *ctx;

	db = talloc(mem_ctx, struct db_context);
	if (db == NULL) {
		return NULL;
	}
	ctx = talloc_zero(db, struct db_cache_ctx);
	if (ctx == NULL) {
		TALLOC_FREE(db);
		return NULL;
	}

	ctx->seqnum = -1;
	ctx->backing = talloc_move(ctx, &backing);
	db->private_data = ctx;
	dbwrap_cache_validate(ctx);

	db->fetch_locked = dbwrap_cache_fetch_locked;
	db->traverse = dbwrap_cache_traverse;
	db->traverse_read = dbwrap_cache_traverse_read;
	db->get_seqnum = dbwrap_cache_get_seqnum;
	db->get_flags = dbwrap_cache_get_flags;
	db->transaction_start = dbwrap_cache_transaction_start;
	db->transaction_commit = dbwrap_cache_transaction_commit;
	db->transaction_cancel = dbwrap_cache_transaction_cancel;
	db->parse_record = dbwrap_cache_parse_record;
	db->exists = dbwrap_cache_exists;
	db->wipe = NULL;
	db->lock_order = 0;
	db->persistent = false;
	return db;
}
