# ruff: noqa: E501,I001
"""add post_blocks, retire post text and post_media position

Revision ID: a24ffa957ea6
Revises: 21445f076be2
Create Date: 2026-08-30 19:42:43.528682

Data-preserving, not a straight drop: `posts.text` and `post_media.position` hold
real content for every existing post, unlike the earlier `has_image` column (which
was always dead). Every post's old (text, then media-in-position-order) content is
converted into `post_blocks` rows in that same order before the columns are dropped,
so nothing existing changes what it looks like once this lands.

Uses `sa.table()`/raw connection queries rather than importing `app.models` - the
usual rule for Alembic data migrations, so this file keeps working unchanged even
after the real models move on.
"""
from alembic import op
import sqlalchemy as sa
import fastapi_users_db_sqlalchemy


# revision identifiers, used by Alembic.
revision = 'a24ffa957ea6'
down_revision = '21445f076be2'
branch_labels = None
depends_on = None


posts_table = sa.table(
    "posts",
    sa.column("id", sa.Integer),
    sa.column("text", sa.Text),
)
post_media_table = sa.table(
    "post_media",
    sa.column("id", sa.Integer),
    sa.column("post_id", sa.Integer),
    sa.column("position", sa.Integer),
)
post_blocks_table = sa.table(
    "post_blocks",
    sa.column("post_id", sa.Integer),
    sa.column("position", sa.Integer),
    sa.column("block_type", sa.String),
    sa.column("text", sa.Text),
    sa.column("media_id", sa.Integer),
)


def _backfill_blocks(conn):
    rows_to_insert = []
    for post_id, text in conn.execute(sa.select(posts_table.c.id, posts_table.c.text)):
        position = 0
        if text:
            rows_to_insert.append(
                {
                    "post_id": post_id,
                    "position": position,
                    "block_type": "text",
                    "text": text,
                    "media_id": None,
                }
            )
            position += 1
        media_rows = conn.execute(
            sa.select(post_media_table.c.id)
            .where(post_media_table.c.post_id == post_id)
            .order_by(post_media_table.c.position)
        )
        for (media_id,) in media_rows:
            rows_to_insert.append(
                {
                    "post_id": post_id,
                    "position": position,
                    "block_type": "media",
                    "text": None,
                    "media_id": media_id,
                }
            )
            position += 1
    if rows_to_insert:
        conn.execute(sa.insert(post_blocks_table), rows_to_insert)


def _unbackfill(conn):
    """Best-effort downgrade: restore posts.text from each post's first text block,
    and post_media.position from each post's media blocks' relative order. Alembic
    downgrades of data migrations are rarely run for real - this just has to not
    raise, not perfectly round-trip.
    """
    text_by_post = {}
    media_position_by_post: dict[int, list[int]] = {}
    result = conn.execute(
        sa.text(
            "SELECT post_id, position, block_type, text, media_id FROM post_blocks ORDER BY post_id, position"
        )
    )
    for post_id, _position, block_type, text, media_id in result:
        if block_type == "text" and post_id not in text_by_post:
            text_by_post[post_id] = text
        elif block_type == "media":
            media_position_by_post.setdefault(post_id, []).append(media_id)

    for post_id, text in text_by_post.items():
        conn.execute(
            posts_table.update().where(posts_table.c.id == post_id).values(text=text)
        )
    for post_id, media_ids in media_position_by_post.items():
        for position, media_id in enumerate(media_ids):
            conn.execute(
                post_media_table.update()
                .where(post_media_table.c.id == media_id)
                .values(position=position)
            )


def upgrade():
    op.create_table('post_blocks',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('post_id', sa.Integer(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('block_type', sa.String(length=10), nullable=False),
    sa.Column('text', sa.String(), nullable=True),
    sa.Column('media_id', sa.Integer(), nullable=True),
    sa.Column('created', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['media_id'], ['post_media.id'], ),
    sa.ForeignKeyConstraint(['post_id'], ['posts.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('post_id', 'position', name='uq_post_block_post_position')
    )
    op.create_index('ix_post_blocks_post_id', 'post_blocks', ['post_id'], unique=False)

    _backfill_blocks(op.get_bind())

    op.drop_constraint('uq_post_media_post_position', 'post_media', type_='unique')
    op.drop_column('post_media', 'position')
    op.drop_column('posts', 'text')


def downgrade():
    # Left nullable, deliberately not matching the original NOT NULL columns: a
    # post with no text block, or a PostMedia row inserted without an owning block
    # (only ever happens via direct-DB test fixtures), has nothing to restore for
    # one of these - best-effort, not a perfect round-trip (see module docstring).
    op.add_column('posts', sa.Column('text', sa.VARCHAR(), autoincrement=False, nullable=True))
    op.add_column('post_media', sa.Column('position', sa.INTEGER(), autoincrement=False, nullable=True))

    _unbackfill(op.get_bind())

    op.create_unique_constraint('uq_post_media_post_position', 'post_media', ['post_id', 'position'])
    op.drop_index('ix_post_blocks_post_id', table_name='post_blocks')
    op.drop_table('post_blocks')
