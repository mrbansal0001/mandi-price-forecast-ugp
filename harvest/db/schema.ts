import {sqliteTable,text,integer,real,primaryKey} from 'drizzle-orm/sqlite-core';
export const series=sqliteTable('series',{key:text('key').primaryKey(),state:text('state').notNull(),district:text('district').notNull(),market:text('market').notNull(),commodity:text('commodity').notNull(),variety:text('variety').notNull(),grade:text('grade').notNull()});
export const prices=sqliteTable('prices',{key:text('key').notNull().references(()=>series.key),date:text('date').notNull(),price:real('price').notNull(),min:real('min'),max:real('max'),source:text('source').notNull(),importId:text('import_id').notNull()},t=>[primaryKey({columns:[t.key,t.date]})]);
export const imports=sqliteTable('imports',{id:text('id').primaryKey(),created:text('created').notNull(),source:text('source').notNull(),rows:integer('rows').notNull(),duplicates:integer('duplicates').notNull(),status:text('status').notNull(),message:text('message')});
export const forecasts=sqliteTable('forecasts',{key:text('key').primaryKey(),revision:integer('revision').notNull(),generated:text('generated').notNull(),result:text('result').notNull()});
export const metadata=sqliteTable('metadata',{key:text('key').primaryKey(),value:integer('value').notNull()});
export const errors=sqliteTable('errors',{id:text('id').primaryKey(),created:text('created').notNull(),context:text('context').notNull(),message:text('message').notNull()});
