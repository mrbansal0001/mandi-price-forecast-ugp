CREATE TABLE `errors` (
	`id` text PRIMARY KEY NOT NULL,
	`created` text NOT NULL,
	`context` text NOT NULL,
	`message` text NOT NULL
);
--> statement-breakpoint
CREATE TABLE `forecasts` (
	`key` text PRIMARY KEY NOT NULL,
	`revision` integer NOT NULL,
	`generated` text NOT NULL,
	`result` text NOT NULL
);
--> statement-breakpoint
CREATE TABLE `imports` (
	`id` text PRIMARY KEY NOT NULL,
	`created` text NOT NULL,
	`source` text NOT NULL,
	`rows` integer NOT NULL,
	`duplicates` integer NOT NULL,
	`status` text NOT NULL,
	`message` text
);
--> statement-breakpoint
CREATE TABLE `metadata` (
	`key` text PRIMARY KEY NOT NULL,
	`value` integer NOT NULL
);
--> statement-breakpoint
CREATE TABLE `prices` (
	`key` text NOT NULL,
	`date` text NOT NULL,
	`price` real NOT NULL,
	`min` real,
	`max` real,
	`source` text NOT NULL,
	`import_id` text NOT NULL,
	PRIMARY KEY(`key`, `date`),
	FOREIGN KEY (`key`) REFERENCES `series`(`key`) ON UPDATE no action ON DELETE no action
);
--> statement-breakpoint
CREATE TABLE `series` (
	`key` text PRIMARY KEY NOT NULL,
	`state` text NOT NULL,
	`district` text NOT NULL,
	`market` text NOT NULL,
	`commodity` text NOT NULL,
	`variety` text NOT NULL,
	`grade` text NOT NULL
);
